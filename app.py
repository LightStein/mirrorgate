#!/usr/bin/env python3
"""
Mirrorgate v3.1.0
Async queue + UI + CI API for mirroring container images
through the corporate proxy into Nexus.

Architecture:
  - skopeo copy (no daemon, runs under restricted SCC)
  - In-memory queue, N worker threads, single-flight per dest:tag
  - JSON API at /api/* (X-API-Key auth) for CI pipelines
  - htmx UI at /ui/* (OAuth proxy in front in OCP) for humans
"""
VERSION = '3.1.3'
import os
import re
import json
import time
import uuid
import queue
import threading
import subprocess
from collections import deque
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, request, jsonify, render_template, Response, redirect

# =============================================================================
# Config
# =============================================================================

NEXUS_REGISTRY = os.environ.get('NEXUS_REGISTRY', '').strip()
NEXUS_USER = os.environ.get('NEXUS_USER', '').strip()
NEXUS_PASS = os.environ.get('NEXUS_PASS', '')
CORPORATE_PROXY = os.environ.get('CORPORATE_PROXY', '').strip()
API_KEY = os.environ.get('API_KEY', '')
PORT = int(os.environ.get('PORT', '8080'))
WORKERS = int(os.environ.get('WORKERS', '3'))
HISTORY_SIZE = int(os.environ.get('HISTORY_SIZE', '500'))
HEALTH_CHECK_INTERVAL = int(os.environ.get('HEALTH_CHECK_INTERVAL', '30'))

if not NEXUS_REGISTRY:
    raise SystemExit(
        "NEXUS_REGISTRY is required. Set it to the destination registry host "
        "(e.g. registry.example.com)."
    )

REGISTRIES = [
    {'name': 'docker.io',           'url': 'https://registry-1.docker.io/v2/'},
    {'name': 'ghcr.io',             'url': 'https://ghcr.io/v2/'},
    {'name': 'quay.io',             'url': 'https://quay.io/v2/'},
    {'name': 'gcr.io',              'url': 'https://gcr.io/v2/'},
    {'name': 'mcr.microsoft.com',   'url': 'https://mcr.microsoft.com/v2/'},
    {'name': 'registry.k8s.io',     'url': 'https://registry.k8s.io/v2/'},
    {'name': 'us-docker.pkg.dev',   'url': 'https://us-docker.pkg.dev/v2/'},
]
REGISTRY_NAMES = {r['name'] for r in REGISTRIES}


def looks_like_registry_host(s):
    """Docker-style heuristic: first path segment is a registry if it contains
    a dot, a colon (port), or is exactly 'localhost'. Otherwise it's a repo path
    on the default registry (docker.io)."""
    return '.' in s or ':' in s or s == 'localhost'

_NO_PROXY_EXTRA = os.environ.get('NO_PROXY_EXTRA', '').strip()
_no_proxy_parts = [NEXUS_REGISTRY, 'localhost', '127.0.0.1']
if _NO_PROXY_EXTRA:
    _no_proxy_parts.extend(p.strip() for p in _NO_PROXY_EXTRA.split(',') if p.strip())
NO_PROXY_HOSTS = ','.join(p for p in _no_proxy_parts if p)

# =============================================================================
# Failure classifier
# =============================================================================

FAILURE_PATTERNS = [
    (r'(connection refused|no route to host|proxyconnect.*refused|proxy.*timeout|could not resolve proxy)',
     'ProxyUnreachable', 'Corporate proxy is unreachable. Check proxy host/port and that it is up.'),
    (r'(unauthor[is]?ed|http 401|requested access to the resource is denied|incorrect username or password|invalid username/password)',
     'AuthFailed', 'Source registry refused credentials. Verify src_user / src_token.'),
    (r'(429|too many requests|rate limit|toomanyrequests)',
     'RateLimited', 'Source registry is rate-limiting. Retry later or use authenticated pull.'),
    (r'(manifest unknown|name unknown|repository name not known|repository .* not found)',
     'ImageNotFound', 'Image does not exist on the source registry.'),
    (r'(manifest .* not found|tag .* not found|reference does not exist)',
     'TagNotFound', 'Image exists but the requested tag does not.'),
    (r'(invalid manifest|blob upload invalid|manifest invalid|unsupported media type|manifest references.*does not exist|denied: requested access to the resource is denied)',
     'NexusRejectedManifest', 'Nexus rejected the manifest. Likely OCI vs Docker-v2 format mismatch or hosted-repo permissions.'),
    (r'(no space left on device|disk full)',
     'DiskFull', 'Out of disk space on the copier pod. Check ephemeral storage.'),
    (r'(deadline exceeded|context deadline exceeded|timed out|i/o timeout|operation timed out)',
     'Timeout', 'Copy timed out. Image may be too large or network slow.'),
]


def classify_failure(stderr: str):
    if not stderr:
        return ('Unknown', 'No error output captured.')
    s = stderr.lower()
    for pattern, cat, hint in FAILURE_PATTERNS:
        if re.search(pattern, s, re.IGNORECASE):
            return (cat, hint)
    return ('Unknown', 'Did not match any known failure pattern. See raw log.')


# =============================================================================
# Job model + store
# =============================================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Job:
    def __init__(self, payload):
        self.id = uuid.uuid4().hex[:12]
        self.created_at = now_iso()
        self.started_at = None
        self.finished_at = None
        self.state = 'queued'
        self.src_registry = payload['src_registry']
        self.src_image = payload['src_image']
        self.src_tag = payload['src_tag']
        self.dest_image = payload['dest_image']
        self.dest_tag = payload['dest_tag']
        self._src_user = payload.get('src_user', '')
        self._src_token = payload.get('src_token', '')
        self.log = ''
        self.error_category = None
        self.error_hint = None
        self.cancel_event = threading.Event()
        self.proc = None

    @property
    def dest_key(self):
        return f"{self.dest_image}:{self.dest_tag}"

    @property
    def src_str(self):
        return f"{self.src_registry}/{self.src_image}:{self.src_tag}"

    @property
    def dest_str(self):
        return f"{NEXUS_REGISTRY}/{self.dest_image}:{self.dest_tag}"

    def to_dict(self):
        return {
            'id': self.id,
            'state': self.state,
            'src': self.src_str,
            'dest': self.dest_str,
            'src_registry': self.src_registry,
            'src_image': self.src_image,
            'src_tag': self.src_tag,
            'dest_image': self.dest_image,
            'dest_tag': self.dest_tag,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
            'error_category': self.error_category,
            'error_hint': self.error_hint,
        }


class JobStore:
    def __init__(self, max_size):
        self._lock = threading.Lock()
        self._jobs = {}
        self._order = deque(maxlen=max_size)
        self._inflight_dest = set()
        self._inflight_lock = threading.Lock()

    def add(self, job):
        with self._lock:
            if len(self._order) == self._order.maxlen and self._order:
                # deque pops left automatically; clean dict
                evicted = self._order[0]
                self._jobs.pop(evicted, None)
            self._jobs[job.id] = job
            self._order.append(job.id)

    def get(self, job_id):
        return self._jobs.get(job_id)

    def list(self, limit=200):
        with self._lock:
            ids = list(self._order)[-limit:]
            return [self._jobs[i] for i in ids if i in self._jobs]

    def claim_dest(self, dest_key):
        with self._inflight_lock:
            if dest_key in self._inflight_dest:
                return False
            self._inflight_dest.add(dest_key)
            return True

    def release_dest(self, dest_key):
        with self._inflight_lock:
            self._inflight_dest.discard(dest_key)


store = JobStore(HISTORY_SIZE)
work_queue = queue.Queue()


# =============================================================================
# Skopeo runner
# =============================================================================

def build_skopeo_env():
    env = os.environ.copy()
    if CORPORATE_PROXY:
        for k in ('HTTPS_PROXY', 'HTTP_PROXY', 'https_proxy', 'http_proxy'):
            env[k] = CORPORATE_PROXY
    env['NO_PROXY'] = NO_PROXY_HOSTS
    env['no_proxy'] = NO_PROXY_HOSTS
    return env


def run_copy(job):
    src = f"docker://{job.src_registry}/{job.src_image}:{job.src_tag}"
    dst = f"docker://{NEXUS_REGISTRY}/{job.dest_image}:{job.dest_tag}"

    cmd = [
        'skopeo', 'copy',
        '--retry-times=2',
        '--multi-arch=all',
        '--dest-tls-verify=false',
    ]
    if job._src_user and job._src_token:
        cmd.append(f'--src-creds={job._src_user}:{job._src_token}')
    if NEXUS_USER:
        cmd.append(f'--dest-creds={NEXUS_USER}:{NEXUS_PASS}')
    cmd.extend([src, dst])

    job.state = 'pulling'
    job.started_at = now_iso()

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=build_skopeo_env(), text=True, bufsize=1,
    )
    job.proc = proc

    out_lines = []
    for line in iter(proc.stdout.readline, ''):
        if job.cancel_event.is_set():
            try:
                proc.terminate()
            except Exception:
                pass
            break
        out_lines.append(line)
        if 'Writing manifest' in line:
            job.state = 'pushing'
    proc.stdout.close()
    rc = proc.wait()
    job.log = ''.join(out_lines)

    if job.cancel_event.is_set():
        job.state = 'cancelled'
    elif rc == 0:
        job.state = 'done'
    else:
        job.state = 'failed'
        cat, hint = classify_failure(job.log)
        job.error_category = cat
        job.error_hint = hint

    job.finished_at = now_iso()


def worker_loop():
    while True:
        job = work_queue.get()
        try:
            if job.cancel_event.is_set():
                job.state = 'cancelled'
                job.finished_at = now_iso()
                continue
            if not store.claim_dest(job.dest_key):
                job.log = (
                    f"Refused: another in-flight copy already targets {job.dest_key}.\n"
                    f"Wait for it to finish or pick a different destination tag.\n"
                )
                job.state = 'failed'
                job.error_category = 'Conflict'
                job.error_hint = (
                    f'Another job is currently copying to {job.dest_key}. '
                    f'Retry once it finishes, or pick a different dest_tag.'
                )
                job.finished_at = now_iso()
                continue
            try:
                run_copy(job)
            finally:
                store.release_dest(job.dest_key)
        except Exception as e:
            job.state = 'failed'
            job.log = (job.log or '') + f"\n[runner crashed] {e}\n"
            job.error_category = 'Unknown'
            job.error_hint = 'Internal runner error. See raw log.'
            job.finished_at = now_iso()
        finally:
            work_queue.task_done()


# =============================================================================
# Registry health checker
# =============================================================================

class RegistryHealth:
    def __init__(self, registries):
        self._registries = registries
        self._state = {
            r['name']: {'state': 'unknown', 'last_checked': None, 'last_error': None}
            for r in registries
        }
        self._lock = threading.Lock()

    def snapshot(self):
        with self._lock:
            return [
                {'name': r['name'], 'url': r['url'], **self._state[r['name']]}
                for r in self._registries
            ]

    def check_one(self, reg):
        cmd = [
            'curl', '-sS', '-o', '/dev/null', '-w', '%{http_code}',
            '--max-time', '10', '--connect-timeout', '5', reg['url'],
        ]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                env=build_skopeo_env(), timeout=15,
            )
            code = r.stdout.strip()
            if code in ('200', '401'):
                state, err = 'green', None
            elif code:
                state, err = 'yellow', f"HTTP {code}"
            else:
                state, err = 'red', (r.stderr or 'no response').strip()[:200]
        except Exception as e:
            state, err = 'red', str(e)[:200]
        with self._lock:
            self._state[reg['name']] = {
                'state': state,
                'last_checked': now_iso(),
                'last_error': err,
            }

    def check_loop(self):
        while True:
            for r in self._registries:
                self.check_one(r)
            time.sleep(HEALTH_CHECK_INTERVAL)


health = RegistryHealth(REGISTRIES)


# =============================================================================
# Flask app
# =============================================================================

app = Flask(__name__, template_folder='templates')


def require_api_key(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if API_KEY and request.headers.get('X-API-Key') != API_KEY:
            return jsonify({'error': 'Unauthorized'}), 401
        return fn(*a, **kw)
    return wrapper


def parse_src_ref(s):
    """Parse a full source reference. Accepts:
        registry/path/image:tag
        path/image:tag           (default registry docker.io)
        image:tag                (default registry docker.io, library/ prefix)
        any of the above without :tag (defaults to :latest)
    Returns (registry, image, tag, error).
    """
    s = (s or '').strip()
    if not s:
        return None, None, None, 'src is required'
    if ':' in s.rsplit('/', 1)[-1]:
        ref, tag = s.rsplit(':', 1)
    else:
        ref, tag = s, 'latest'
    if '/' in ref:
        head, _, rest = ref.partition('/')
        if looks_like_registry_host(head):
            registry, image = head, rest
        else:
            registry, image = 'docker.io', ref
    else:
        registry, image = 'docker.io', f'library/{ref}'
    if not image:
        return None, None, None, f"can't parse src '{s}'"
    return registry, image, tag, None


def parse_dest_ref(s, default_image, default_tag):
    """Parse a destination reference. Accepts:
        path/image:tag
        path/image          (default tag = src tag)
        empty               (default = mirror src)
    Nexus is always the host; only path+tag matter.
    Returns (image, tag, error).
    """
    s = (s or '').strip()
    if not s:
        return default_image, default_tag, None
    if ':' in s.rsplit('/', 1)[-1]:
        image, tag = s.rsplit(':', 1)
    else:
        image, tag = s, default_tag
    if not image:
        return None, None, f"can't parse dest '{s}'"
    return image, tag, None


def parse_copy_payload(data):
    """Build a job payload from {src, dest?, src_user?, src_token?}.
    Both 'src' and 'dest' are full image refs.
    """
    if not data or not isinstance(data, dict):
        return None, 'JSON body required'
    registry, image, tag, err = parse_src_ref(data.get('src'))
    if err:
        return None, err
    dest_image, dest_tag, err = parse_dest_ref(data.get('dest'), image, tag)
    if err:
        return None, err
    return {
        'src_registry': registry,
        'src_image': image,
        'src_tag': tag,
        'src_user': (data.get('src_user') or '').strip(),
        'src_token': data.get('src_token') or '',
        'dest_image': dest_image,
        'dest_tag': dest_tag,
    }, None


# ----- Health (no auth) -----

@app.get('/health')
def http_health():
    return jsonify({'status': 'healthy', 'version': VERSION, 'workers': WORKERS})


@app.get('/')
def root():
    return redirect('/ui/')


# ----- CI-facing JSON API (X-API-Key) -----

@app.post('/api/copy')
@require_api_key
def api_copy():
    payload, err = parse_copy_payload(request.get_json(silent=True))
    if err:
        return jsonify({'error': err}), 400
    job = Job(payload)
    store.add(job)
    work_queue.put(job)
    return jsonify({'jobId': job.id, 'state': job.state, 'dest': job.dest_str}), 202


@app.get('/api/jobs')
@require_api_key
def api_jobs():
    return jsonify([j.to_dict() for j in store.list()])


@app.get('/api/jobs/<job_id>')
@require_api_key
def api_job(job_id):
    j = store.get(job_id)
    if not j:
        return jsonify({'error': 'not found'}), 404
    return jsonify(j.to_dict())


@app.get('/api/jobs/<job_id>/log')
@require_api_key
def api_job_log(job_id):
    j = store.get(job_id)
    if not j:
        return ('not found', 404)
    return Response(j.log, mimetype='text/plain')


@app.post('/api/jobs/<job_id>/cancel')
@require_api_key
def api_job_cancel(job_id):
    j = store.get(job_id)
    if not j:
        return jsonify({'error': 'not found'}), 404
    if j.state in ('done', 'failed', 'cancelled'):
        return jsonify({'error': f'job already {j.state}'}), 409
    j.cancel_event.set()
    if j.proc:
        try:
            j.proc.terminate()
        except Exception:
            pass
    return jsonify({'jobId': j.id, 'state': 'cancelling'})


@app.post('/api/jobs/<job_id>/retry')
@require_api_key
def api_job_retry(job_id):
    j = store.get(job_id)
    if not j:
        return jsonify({'error': 'not found'}), 404
    if j.state not in ('failed', 'cancelled'):
        return jsonify({'error': f'cannot retry a {j.state} job'}), 409
    new_payload = {
        'src_registry': j.src_registry,
        'src_image': j.src_image,
        'src_tag': j.src_tag,
        'src_user': j._src_user,
        'src_token': j._src_token,
        'dest_image': j.dest_image,
        'dest_tag': j.dest_tag,
    }
    new_job = Job(new_payload)
    store.add(new_job)
    work_queue.put(new_job)
    return jsonify({'jobId': new_job.id, 'state': new_job.state}), 202


@app.get('/api/registries')
def api_registries():
    return jsonify(health.snapshot())


# ----- htmx UI (OAuth proxy handles auth in OCP) -----

@app.get('/ui/')
@app.get('/ui')
def ui_index():
    return render_template(
        'index.html',
        registries=health.snapshot(),
        registry_names=[r['name'] for r in REGISTRIES],
        nexus=NEXUS_REGISTRY,
        jobs=list(reversed(store.list(limit=100))),
        version=VERSION,
    )


@app.get('/ui/strip')
def ui_strip():
    return render_template('_strip.html', registries=health.snapshot())


@app.get('/ui/board')
def ui_board():
    return render_template('_board.html', jobs=list(reversed(store.list(limit=100))))


@app.post('/ui/jobs/<job_id>/cancel')
def ui_cancel(job_id):
    j = store.get(job_id)
    if j and j.state in ('queued', 'pulling', 'pushing'):
        j.cancel_event.set()
        if j.proc:
            try:
                j.proc.terminate()
            except Exception:
                pass
    return render_template('_board.html', jobs=list(reversed(store.list(limit=100))))


@app.post('/ui/jobs/<job_id>/retry')
def ui_retry(job_id):
    j = store.get(job_id)
    if j and j.state in ('failed', 'cancelled'):
        new_payload = {
            'src_registry': j.src_registry,
            'src_image': j.src_image,
            'src_tag': j.src_tag,
            'src_user': j._src_user,
            'src_token': j._src_token,
            'dest_image': j.dest_image,
            'dest_tag': j.dest_tag,
        }
        new_job = Job(new_payload)
        store.add(new_job)
        work_queue.put(new_job)
    return render_template('_board.html', jobs=list(reversed(store.list(limit=100))))


@app.post('/ui/copy')
def ui_copy():
    raw = {k: request.form.get(k, '') for k in
           ('src', 'dest', 'src_user', 'src_token')}
    parsed, err = parse_copy_payload(raw)
    if not err:
        job = Job(parsed)
        store.add(job)
        work_queue.put(job)
    # Always return both: board replaces #board (target), OOB swap updates #form-error.
    err_html = render_template('_form_error.html', error=err)
    board_html = render_template('_board.html', jobs=list(reversed(store.list(limit=100))))
    return err_html + board_html


@app.get('/ui/jobs/<job_id>/log')
def ui_log(job_id):
    j = store.get(job_id)
    if not j:
        return render_template('_drawer.html', job=None)
    return render_template('_drawer.html', job=j)


@app.get('/ui/drawer/close')
def ui_drawer_close():
    return ''


# =============================================================================
# Boot
# =============================================================================

def main():
    print(f"Mirrorgate v{VERSION} :: nexus={NEXUS_REGISTRY} proxy={CORPORATE_PROXY} "
          f"workers={WORKERS} api_key={'on' if API_KEY else 'off'}", flush=True)
    for _ in range(WORKERS):
        threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=health.check_loop, daemon=True).start()
    # Flask's built-in server with threading; one process keeps in-memory state coherent.
    app.run(host='0.0.0.0', port=PORT, threaded=True)


if __name__ == '__main__':
    main()
