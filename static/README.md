# Vendored assets

## `htmx.min.js`

htmx 1.9.12, vendored rather than loaded from a CDN.

Before v3.4.0 `index.html` pulled it from `unpkg.com` at page load. That made
the entire UI depend on the browser reaching the public internet - and in a
bank, where the browser goes out through a corporate proxy that may not allow
unpkg, a single blocked request left the page rendered but completely inert:
no polling, no form submit, no drawer, nothing. Nothing in the pod's own logs
would show why.

Serving it from the image removes that dependency and pins the exact bytes.

* Source: <https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js>
* sha256: `449317ade7881e949510db614991e195c3a099c4c791c24dacec55f9f4a2a452`
* Licence: Zero-Clause BSD, see `HTMX-LICENSE.txt`

To update, replace the file, record the new version and checksum here, and
check the UI still polls (the jobs board refreshes every 2s) and that the
history filter, copy buttons and re-run all still work.
