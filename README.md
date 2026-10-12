# Mac Cleaner

A free, open-source storage cleaner for macOS, inspired by CleanMyMac. It finds junk, hidden “System Data”, unused apps and big files, then cleans them up for you.

**[⬇ Download Mac Cleaner (zip)](https://github.com/armenkazarian8-wq/mac-cleaner/releases/latest/download/MacCleaner.zip)**

Works on macOS 13 Ventura and later, on both Apple silicon and Intel Macs.

## Features

| Section | What it does |
|---|---|
| **Smart Scan** | One button: finds junk, unused apps and large files, then cleans the junk. |
| **System Junk** | User and system caches, logs, temporary files, browser caches, developer caches, leftover installers. |
| **System Data** | The hidden space macOS calls “System Data”: iPhone simulators and simulator runtimes, Docker disk images, iPhone/iPad backups, iOS updates, Xcode archives, macOS installers, Mail attachments, Time Machine local snapshots. |
| **Trash Bins** | Empties the Trash on your Mac and on connected drives. |
| **Optimization** | Lists programs that start automatically in the background, and spots broken ones left by removed apps. |
| **Maintenance** | Free up RAM, flush DNS, thin Time Machine snapshots, rebuild Launch Services, reset Quick Look, clear font caches, reindex Spotlight. |
| **Uninstaller** | Removes apps *with* their leftover files, finds apps you haven't opened in months, and cleans leftovers from apps you already deleted. |
| **Space Lens** | Maps every file and folder in one scan, then lets you browse instantly to see what's taking up space. |
| **Large & Old Files** | Big files and downloads you haven't opened in a while. |
| **Duplicates** | Identical copies of files, compared by content. |

## Install

1. [Download `MacCleaner.zip`](https://github.com/armenkazarian8-wq/mac-cleaner/releases/latest/download/MacCleaner.zip) and double-click it to unzip.
2. Drag **Mac Cleaner.app** into your **Applications** folder.
3. Open it. Because the app isn't notarized by Apple (that needs a paid developer account), macOS will block it the first time:
   - Go to **System Settings → Privacy & Security**, scroll down and click **Open Anyway** next to “Mac Cleaner”, then confirm.
   - *Or* run this once in Terminal:
     ```sh
     xattr -dr com.apple.quarantine "/Applications/Mac Cleaner.app"
     ```
4. If macOS asks to install the **Command Line Tools**, click **Install**. The scanning engine runs on the Python 3 that comes with them. It's a free download from Apple, and Macs with Xcode installed already have it.

**Optional:** to let Mac Cleaner also scan Mail attachments, device backups and other protected folders, turn it on under **System Settings → Privacy & Security → Full Disk Access**.

## Is it safe?

- **Nothing is removed without your confirmation.** Every scan shows you exactly what it found, and you choose what to clean.
- **Regenerable junk** (caches, logs, temp files) is deleted outright so the space is freed immediately. **Anything personal** (apps, documents, backups, large files) goes to the **Trash**, so you can put it back.
- It only removes items that appeared in your most recent scan, and never touches macOS's own protected system files.
- System items (system caches, snapshots, background daemons) need your password, entered in the normal macOS prompt.
- Apple's own apps are hidden from the Uninstaller.
- Everything runs locally. Nothing is uploaded anywhere.

## How it works

The app is a small native window (Swift + WebKit) around a local cleaning engine written in Python. The engine runs on your Mac only, on `127.0.0.1` with a random port and a per-launch access token.

```
main.swift   native app window, menus, starts the engine
cleaner.py   scanning and cleaning engine (Python 3, no dependencies)
ui.html      the interface
icon.swift   draws the app icon at build time
build.sh     builds the universal .app, zips it, and installs it
```

You can also run the engine without the app: `python3 cleaner.py` opens the same interface in your browser.

## Build from source

Requires Xcode or the Command Line Tools.

```sh
git clone https://github.com/armenkazarian8-wq/mac-cleaner.git
cd mac-cleaner
./build.sh               # builds, zips to dist/MacCleaner.zip, installs to /Applications
./build.sh --no-install  # only build and zip
```

## License

[MIT](LICENSE)
