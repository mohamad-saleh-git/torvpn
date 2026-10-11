# torvpn

A small macOS command-line tool that connects to the [Tor](https://www.torproject.org/) network, using **Snowflake** or **obfs4** bridges so it can work where Tor is blocked, and routes your Mac's traffic through Tor via the system SOCKS proxy.

No server, VPS or account required. One Python file, no third-party Python dependencies.

> **Not a real VPN.** torvpn sets the macOS system SOCKS proxy. Apps that respect the system proxy (Safari, Chrome, most GUI apps) go through Tor. Apps that ignore it (some CLI tools, UDP traffic) connect directly. See [Limitations](#limitations).

## Features

- One-command setup of Tor, Snowflake and obfs4 via Homebrew
- Connection modes: **Snowflake** (default), **obfs4**, or **direct** (no bridge)
- Live bootstrap progress, including relay-descriptor download progress
- Saves your existing proxy settings and restores them exactly on disconnect
- Checks the exit IP and confirms traffic really goes through Tor
- Bridge tester: ranks obfs4 bridges by reachability and saves the working ones

## Requirements

- macOS (tested with Apple Silicon and Homebrew in `/opt/homebrew`)
- [Homebrew](https://brew.sh)
- Python 3.8+ (the one that ships with macOS developer tools works)
- Administrator password (needed to change the system proxy)

## Install

```bash
git clone https://github.com/mohamad-saleh-git/torvpn
cd torvpn
python3 torvpn.py setup
```

`setup` runs `brew install` for `tor`, `snowflake` and `lyrebird` (falling back to `obfs4proxy` if `lyrebird` is not available).

## Usage

```bash
python3 torvpn.py connect                # Snowflake bridge (default)
python3 torvpn.py connect -m obfs4       # obfs4 bridges from ~/.torvpn/bridges.txt
python3 torvpn.py connect -m direct      # no bridge (only if Tor is not blocked)
python3 torvpn.py status                 # is Tor running, and what is the exit IP?
python3 torvpn.py disconnect             # stop Tor and restore your proxy settings
python3 torvpn.py test-bridges FILE      # test and rank obfs4 bridges
```

| Option | Description |
| --- | --- |
| `-m`, `--mode` | `snowflake` (default), `obfs4` or `direct` |
| `-t`, `--timeout` | Seconds without any progress before giving up (default `300`) |
| `--no-proxy` | Start Tor only; do not touch the system proxy. Tor listens on `127.0.0.1:9050` |

### Safe first test

Try Tor without changing any system settings:

```bash
python3 torvpn.py connect --no-proxy
python3 torvpn.py status
curl --socks5-hostname 127.0.0.1:9050 https://check.torproject.org/api/ip
python3 torvpn.py disconnect
```

The last `curl` should return `"IsTor":true`.

## Bridges

If Tor is blocked on your network, you need a bridge.

**Snowflake** works out of the box with a built-in bridge line. Bridge details change over time; if it stops working, replace it with a current line. You can copy `snowflake ...` lines from Tor Browser's `pt_config.json`:

```
/Applications/Tor Browser.app/Contents/Resources/TorBrowser/Tor/PluggableTransports/pt_config.json
```

**obfs4** needs bridge lines you request yourself:

1. Get bridges from <https://bridges.torproject.org> (or email `bridges@torproject.org`).
2. Save them to a file, one per line, e.g. `my-bridges.txt`.
3. Test and rank them:
   ```bash
   python3 torvpn.py test-bridges my-bridges.txt
   ```
   Working bridges are saved to `~/.torvpn/bridges.txt`, fastest first. Note this only checks that the port is reachable, not that the bridge fully works.
4. Connect: `python3 torvpn.py connect -m obfs4`

You can also put `snowflake` or `obfs4` lines (with or without a leading `Bridge `) directly into `~/.torvpn/bridges.txt`.

## Where files are stored

Everything lives in `~/.torvpn/`:

| File | Purpose |
| --- | --- |
| `torrc` | Generated Tor configuration |
| `tor.log` | Tor log (useful for debugging) |
| `data/` | Tor data directory and cached relay info |
| `bridges.txt` | Your bridge lines |
| `state.json` | Your original proxy settings, restored on disconnect |

## Troubleshooting

**Stuck at "Bootstrap 50%".** This stage downloads information about ~9000 relays, which is slow over Snowflake. As long as the descriptor counter keeps increasing, just wait. Follow progress with:

```bash
grep -E "microdescriptors|Bootstrapped" ~/.torvpn/tor.log | tail -3
```

If it truly makes no progress, run `disconnect` and `connect` again (Snowflake picks a new volunteer proxy each time), try `-t 600`, or switch to `-m obfs4`.

**Internet stops working after a crash.** Run `python3 torvpn.py disconnect`. If that does not help, turn the proxy off manually:

```bash
networksetup -listallnetworkservices
sudo networksetup -setsocksfirewallproxystate "Wi-Fi" off
```

## Limitations

- **System proxy, not a tunnel.** Only apps that honor the macOS SOCKS proxy are routed through Tor. There is no kill switch: if Tor stops, those apps may fall back to direct connections once the proxy is removed.
- **Not anonymity on its own.** Use Tor Browser for anonymous browsing; it also defends against browser fingerprinting, which this tool does not.
- **Speed.** Tor is slow by design. It is not suited for torrenting or large downloads.
- **Some sites block Tor exit nodes.**
- Only tested on macOS.

## Uninstall

```bash
python3 torvpn.py disconnect
rm -rf ~/.torvpn
brew uninstall tor snowflake lyrebird obfs4proxy
```

## Disclaimer

Provided as is, without warranty. Check the laws that apply to you before using Tor or circumvention tools.
