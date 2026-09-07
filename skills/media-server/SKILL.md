---
name: media-server
description: Plan or implement local media-server setups such as Jellyfin with correct GPU, network, and storage assumptions.
---

# Media Server

Use this skill for Jellyfin, Plex-like media servers, transcoding, media libraries, storage paths, client devices, and home-network deployment.

Core behavior:

- Put the machine with the GPU in charge of transcoding. A Raspberry Pi can be a client, controller, storage helper, reverse proxy, or SSH-managed node, but it cannot use a PC's RTX 4090 through plain USB.
- For Jellyfin on Windows or Linux, identify the install method, media paths, hardware acceleration mode, firewall exposure, and backup location.
- Prefer LAN-only exposure unless the user explicitly asks for remote internet access.
- For internet exposure, recommend a reverse proxy or VPN/Tailscale-style tunnel with authentication and TLS.
- Validate success with a playback test, dashboard transcoding indicator, and logs.

Keep recommendations hardware-specific and avoid pretending that generic USB gives GPU or shell control.
