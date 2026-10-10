# Shared standalone launcher source

The full native launcher source is versioned in both standalone repositories:

- `app-engine/desktop-app` (maintenance source)
- `app-store/desktop-app` (synchronized source)

Neither copy depends on Atrium. Both launch App Engine and App Store. macOS uses
the Swift/AppKit sources here; Windows uses `windows/*.cs` and the PowerShell
build/install scripts. Build from either checkout with sibling repositories, or
supply explicit repository paths.

After editing the maintenance source, synchronize and check the second copy:

```sh
python3 desktop-app/sync.py --target /path/to/app-store
python3 desktop-app/sync.py --target /path/to/app-store --check
```

Running from App Store requires `--source /path/to/app-engine` if the checkouts
aren't siblings. Build products and caches are excluded. The sync command never
removes destination files or copies other repository content. Commit the
resulting source changes in both repositories.
