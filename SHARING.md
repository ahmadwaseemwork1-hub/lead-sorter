# Sharing Leads Sorter with your team (same network, no internet needed)

One person (the **host**) runs the app; everyone else uses it from their own
browser. Nobody installs anything. Nothing leaves your network.

## 1. Host: start the app

- **Windows:** double-click `start_server.bat`
- **Linux/Mac:** run `./start_server.sh`

A window opens and prints something like:

```
  On this machine:           http://127.0.0.1:8080
  Colleagues (same network): http://192.168.1.50:8080
```

Keep that window open — closing it stops the app for everyone.
If port 8080 is busy it automatically picks the next free one and tells you.

## 2. Host: allow it through the firewall (one time)

**Windows:** the first launch usually shows a "Windows Defender Firewall"
popup — tick **Private networks** and click **Allow access**. If you missed
it: Start → "Allow an app through firewall" → Change settings → Allow
another app → browse to your Python, allow on Private.
Or in an admin PowerShell:

```
netsh advfirewall firewall add rule name="Leads Sorter" dir=in action=allow protocol=TCP localport=8080
```

**Linux (ufw):** `sudo ufw allow 8080/tcp`

## 3. Colleagues: connect

1. Make sure you're on the **same Wi-Fi / office network** as the host
   (a VPN on either machine can break this — turn it off or use split tunneling).
2. Open the `http://192.168.x.x:PORT` address the host gives you, in any browser.
3. Drag your leads file (CSV, TSV, or Excel) onto the upload box, click
   **Organize leads**, then download the cleaned CSV, the change report,
   and the error log.

If it doesn't load:
- Re-check the address (it changes if the host's machine gets a new IP).
- Host: confirm the app window is still open and shows no errors.
- Host: re-check the firewall rule (step 2).
- Ping test from the colleague's machine: `ping 192.168.x.x`.

## 4. Options (host edits `server_config.json`, then restarts)

| Setting | Default | Meaning |
|---|---|---|
| `port` | 8080 | Port to serve on |
| `passphrase` | "" (off) | When set, uploading requires this passphrase |
| `retention_hours` | 24 | Uploaded/processed files are auto-deleted after this |
| `max_upload_mb` | 50 | Uploads bigger than this are rejected with a clear message |
| `host` | 0.0.0.0 | `0.0.0.0` = reachable on the LAN; `127.0.0.1` = this machine only |

## Notes

- Files are processed and stored **only on the host machine** and deleted
  after the retention period.
- Everything works with the internet unplugged — no cloud, no CDN.
- Two people can upload at the same time; every upload gets its own job ID,
  so results never mix.
- Finding the host's IP manually: Windows `ipconfig` (look for "IPv4
  Address"), Linux/Mac `ip addr` or `ifconfig`.
