# {{APP_LABEL}}

This template is deliberately language neutral. Supply an executable named
`server` at the project root. App Engine launches it without a shell and sets:

- `PORT`: assigned loopback HTTP port
- `APP_ID`: `{{APP_ID}}`
- `APP_INSTANCE_ID`: scoped runtime instance
- `APP_DATA_DIR`: writable persistent data directory
- `APP_BASE_URL`: the app's public base URL

The process must listen on `127.0.0.1:$PORT`, return HTTP success from
`GET /health`, and remain in the foreground. Keep language-specific install or
build commands as argv arrays in `app.json`; never use shell command strings.
