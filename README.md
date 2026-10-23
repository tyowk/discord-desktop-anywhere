# Discord Desktop in Browser

Run the **official Discord PTB desktop client** directly in your browser using noVNC — powered by Replit, with no local PC or Docker required.

This project is primarily designed for **Replit's Linux x86_64 environment**, but it may also be adapted for other Linux environments, including VPS instances and cloud VMs.

* Run the Discord desktop client instead of Discord Web or the mobile app.
* Access Chromium/Electron DevTools for console inspection, element inspection, and development experiments.
* Connect remotely from a phone, tablet, or computer through a browser.
* Use a virtual desktop without a physical monitor or dedicated GPU.

## How It Works

```text
Browser (Phone / PC)
        |
      noVNC
        |
   WebSocket Proxy
        |
   Replit (Linux x86_64)
        ├── Xvfb          Virtual display
        ├── Openbox       Window manager
        ├── x11vnc        VNC server
        ├── websockify    WebSocket bridge
        └── Discord PTB   Electron client
```

The desktop session runs inside Replit's Linux environment. noVNC provides browser-based access to the remote desktop, allowing you to interact with Discord from another device.

The provided setup uses Replit's Nix environment and does not require Docker or root access.

## Project Structure

| File         | Purpose                                                                                      |
| ------------ | -------------------------------------------------------------------------------------------- |
| `start.sh`   | Downloads Discord, prepares the runtime, starts the virtual desktop, and launches the client |
| `replit.nix` | Defines the required packages and runtime libraries for Replit                               |
| `.replit`    | Configures the run command and Replit web ports                                              |
| `control.py` | Provides a mobile-friendly control panel for DevTools and common actions                     |

## Requirements

* A Replit account with a Linux x86_64 environment.
* Sufficient memory and CPU resources to run Discord PTB.
* A modern browser on the device used to connect.
* A `VNC_PASSWORD` secret for protecting remote desktop access.
* Network access to download Discord PTB and its dependencies.

The free Replit environment may have limited resources, which can cause slow performance or crashes.

## Installation

### 1. Create a Repl

Create a new Repl using a Bash or Blank template.

### 2. Add the Project Files

Place the following files in the root of your Repl:

* `start.sh`
* `replit.nix`
* `.replit`
* `control.py`

Make sure `start.sh` is executable if your setup requires it.

### 3. Configure Secrets

Open **Secrets** in Replit and add:

| Secret         | Value                            |
| -------------- | -------------------------------- |
| `VNC_PASSWORD` | A strong password of your choice |

Do not share this password or expose your remote desktop without authentication.

### 4. Start the Project

Click **Run** and wait for the initial setup to finish. On the first launch, the script downloads the Discord installer and retrieves the full client, so startup may take several minutes.

### 5. Open Discord

Open the Replit **Webview** and navigate to:

```text
/vnc.html?autoconnect=true&resize=scale
```

Enter your VNC password when prompted, then log in to Discord.

## Using It on a Phone

1. Open the noVNC page in your mobile browser.
2. Rotate your phone to landscape mode.
3. Open the noVNC settings and select **Local Scaling**.
4. Use the on-screen keyboard and extra-key controls to send keyboard shortcuts.
5. Interact with the remote desktop using touch input.

### DevTools and Control Panel

The startup script attempts to enable DevTools and open them automatically after Discord launches.

The separate control panel provides shortcuts for:

* Opening or closing DevTools
* Opening the DevTools Console
* Reloading the client
* Hard reloading the client
* Sending Escape

In the provided Replit configuration, the control panel listens on port `6081`, which may be exposed through Replit's web port mapping.

To disable automatic DevTools opening, configure the environment variable:

```text
AUTO_DEVTOOLS=0
```

## Configuration

| Variable        | Default                       | Description                                               |
| --------------- | ----------------------------- | --------------------------------------------------------- |
| `VNC_PASSWORD`  | Empty                         | Password for VNC access; configure this in Replit Secrets |
| `RESOLUTION`    | `960x540x24`                  | Virtual display resolution                                |
| `AUTO_DEVTOOLS` | `1`                           | Set to `0` to disable automatic DevTools opening          |
| `PTB_URL`       | Official Discord download URL | Overrides the installer download URL                      |

The included script also uses fixed internal ports for VNC and noVNC. Replit's external port mapping may differ from the ports used inside the environment.

## Troubleshooting

| Problem                              | Possible solution                                                                           |
| ------------------------------------ | ------------------------------------------------------------------------------------------- |
| `exit 127` or executable not found   | Check executable paths, the glibc loader, and runtime dependencies                          |
| Missing shared library (`libXXX.so`) | Check the required Nix packages and `LD_LIBRARY_PATH`                                       |
| Display is cropped on mobile         | Enable Local Scaling in noVNC settings                                                      |
| Discord crashes frequently           | Check memory availability and CPU usage                                                     |
| Clean application installation       | Remove `.discord-novnc/app`; preserve `.discord-novnc/profile` to retain local session data |
| Startup fails                        | Inspect the logs in `.discord-novnc/logs/`                                                  |
| DevTools does not open               | Check the Discord window detection and `AUTO_DEVTOOLS` setting                              |

## Limitations

* **Audio and voice chat:** Audio forwarding is not configured in the default setup.
* **Software rendering:** GPU acceleration is disabled, which may reduce performance.
* **Resource usage:** Discord is an Electron application and may consume significant memory and CPU resources.
* **Session persistence:** Replit sessions may stop due to platform limits or inactivity.
* **Updates:** Discord PTB updates and runtime compatibility may require maintenance.
* **Platform compatibility:** The included configuration is designed for Replit and is not guaranteed to work unchanged elsewhere.

## Using Other Linux Environments

Although Replit is the primary target, the project may be adapted for other Linux x86_64 environments, such as Ubuntu, Debian, VPS instances, or cloud VMs.

Such environments may require changes to:

* Package installation and dependency configuration
* noVNC asset locations
* glibc loader paths and executable patching
* Display and service startup commands
* Network ports, reverse proxies, and authentication

The included `replit.nix` and `.replit` files are Replit-specific. Other environments must provide equivalent dependencies and configuration.

## Security and Discord Terms

* Always configure a strong `VNC_PASSWORD`.
* Do not expose VNC or the control panel publicly without appropriate access controls.
* Use HTTPS and authenticated access when exposing the web interface through a reverse proxy.
* Never share session URLs, passwords, or unauthenticated control endpoints.
* DevTools access is intended for inspection, learning, and legitimate development experiments.
* Self-bots and unauthorized client modifications may violate [Discord's Terms of Service](https://discord.com/terms).

## Disclaimer

This is an independent community project and is not affiliated with, endorsed by, or sponsored by Discord Inc. or Replit.

Discord is a trademark of Discord Inc. All product names are used for identification purposes only.
