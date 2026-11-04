# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Configurable live status check (`COMMAND_LIMIT_STATUS`, optional `COMMAND_LIMIT_STATUS_RUNNING_PATTERN`) so that while a traffic/storage/API limit is active, the stop command is re-run if the managed app (e.g. AutoBRR) comes back up after a node reboot.
- Ultra.cc-compatible user systemd unit at `deploy/seedbox-torrent-manager.service`, with install/enable instructions in the README (no `screen` / `sudo` required).
- Project changelog (`CHANGELOG.md`).

### Changed

- **Server mode:** failed `quota` / `app-traffic` checks no longer stop the managed app and no longer abort startup. The script keeps retrying each cycle and sends Discord alerts at most once per hour until the commands succeed again.
- Remote mode Ultra API outages still run the stop command as a precaution; recovery behaviour is unchanged.
