# storage-interface

One **async** Python interface for many cloud storage services. Write your
application against `StorageBackend` once; swap or add services without
touching application code.

Built-in backends: **Google Drive**, **Telegram**, and **local disk** (reference
implementation, handy for tests and staging). New backends plug in through a
registry.

```python
import asyncio
from storage_interface import create_backend, copy_between

async def main():
    async with create_backend("gdrive", token_file="token.json",
                              client_secrets_file="client_secret.json") as drive, \
               create_backend("telegram", api_id=12345, api_hash="...",
                              channel="my_storage_channel",
                              index_path="tg_index.sqlite") as tg:

        await drive.upload_file("report.pdf", "/work/2026/report.pdf")
        async for item in drive.iterdir("/work/2026"):
            print(item.path, item.size)

        # Drive -> Telegram, streamed through one temp file
        await copy_between(drive, "/work/2026/report.pdf", tg, "/backups/report.pdf")

asyncio.run(main())
```

## Install

```bash
pip install "storage-interface[all]"        # or: storage-interface[gdrive] / storage-interface[telegram]
```

The core has no dependencies; each backend's libraries are optional extras and
are only imported when you use that backend.

## The interface

Paths are POSIX-style and absolute within the backend (`/photos/2026/a.jpg`),
whatever the service does internally. `..` is rejected.

| Method | Notes |
|---|---|
| `stat(path)` | `FileInfo` (`path, name, is_dir, size, modified, mime_type, backend_id`) |
| `iterdir(path)` / `listdir(path)` / `walk(path)` | direct children, as a list, or recursive |
| `mkdir(path, parents=True, exist_ok=True)` | |
| `upload_file(local, remote, overwrite=False, progress=None)` | creates missing parent folders |
| `download_file(remote, local, overwrite=False, progress=None)` | atomic: writes `*.part`, then renames |
| `read_bytes(path)` / `write_bytes(path, data)` | small blobs |
| `delete(path, recursive=False)` | |
| `move(src, dst)` / `copy(src, dst)` | optional; check `backend.supports(Capability.MOVE)` |
| `exists(path)` | |

`copy_between(src, path, dst, path)` and `copy_tree(...)` move data between any
two backends. All failures are `storage_interface.StorageError` subclasses
(`NotFoundError`, `AlreadyExistsError`, `AuthError`, `RateLimitError`, ...), so
application code never catches provider-specific exceptions.

`progress(done, total)` callbacks may run on a worker thread; keep them quick.

## Google Drive

`create_backend("gdrive", ...)` takes one of:

* `token_file=` (+ `client_secrets_file=` on the first run): OAuth for a normal
  Google account. Create an OAuth client ("Desktop app") in Google Cloud
  Console, enable the Drive API; the first run opens a browser and saves a
  refreshable token.
* `service_account_file=`: server-to-server. Share the target folder with the
  service account's e-mail (service accounts have their own, empty Drive).
* `credentials=`: any `google.auth` credentials object.

Useful options: `root_folder_id=` (confine the app to one folder),
`trash_on_delete=True` (deletes are recoverable), `scopes=` (defaults to full
Drive access; `drive.file` limits you to files the app created).

Things to know:

* Drive allows duplicate names in one folder; the **oldest** match wins.
* Folder ids are cached. If you reorganise Drive elsewhere, call `clear_cache()`.
* Google Docs/Sheets/Slides are exported on download (to docx/xlsx/pptx; override
  with `export_formats=`). Drive cannot `copy` folders; use `copy_tree`.
* API calls are serialised behind a lock (the Google client is not thread-safe).

## Telegram

Telegram has no folders or listings, so the backend builds a filesystem on top
of it: each file is a document message in one chat/channel, the path is stored
in the message **caption**, and a local SQLite **index** provides `stat`,
`iterdir`, folders and `move`.

```python
create_backend("telegram",
    api_id=12345, api_hash="...",        # from https://my.telegram.org
    channel="my_channel",                # "me" = Saved Messages (default)
    session="my_session",                # Telethon session file
    index_path="tg_index.sqlite",        # keep this file: it is your directory tree
    bot_token=None)                      # or log in as a bot (must be a channel admin)
```

The first `connect()` as a user asks for your phone number and login code on
the terminal; the session file is reused afterwards.

Things to know:

* Limit is **2000 MiB per file** (4000 MiB for Premium, set `max_file_size=`).
  Empty files are rejected. Files are *not* split into parts yet.
* If the index is lost, `await backend.rebuild_index()` reconstructs it from the
  captions (empty folders are not recoverable). Edit/deletes made directly in
  Telegram are not seen until you rebuild.
* `move` only rewrites the index and edits captions (best effort), so it never
  re-uploads. There is no server-side `copy`.
* Heavy automated use of a *user* account can hit flood limits; Telethon sleeps
  through short ones. Check Telegram's terms for your use case.
* Don't share one index file between processes writing concurrently.

## Adding a backend

Subclass `StorageBackend`, implement `stat`, `iterdir` (async generator),
`mkdir`, `upload_file`, `download_file`, `delete` (and optionally `move`/`copy`),
then either call `register_backend("dropbox", DropboxBackend)` or ship an entry
point so `create_backend("dropbox")` works once the package is installed:

```toml
[project.entry-points."storage_interface.backends"]
dropbox = "my_pkg.dropbox:DropboxBackend"
```

Services with no real folder tree can reuse `storage_interface.backends._pathindex.PathIndex`
as the Telegram backend does. `LocalBackend` is the smallest complete example.

## Development

```bash
pip install -e ".[dev,all]"
pytest
```

The Drive and Telegram tests run against in-memory fakes of the Drive v3 and
Telethon APIs, so they need no credentials or network.
