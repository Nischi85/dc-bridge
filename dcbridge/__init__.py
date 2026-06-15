"""dc-bridge — AirDC++ ↔ Sonarr/Radarr/Jellyseerr search bridge.

Package split out of the original single-file bridge.py. Modules are imported in
dependency order: config → helpers → state → airdcpp → arr → poller → web.
"""
