# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Configurable live status check (`COMMAND_LIMIT_STATUS`, optional `COMMAND_LIMIT_STATUS_RUNNING_PATTERN`) so that while a traffic/storage/API limit is active, the stop command is re-run if the managed app (e.g. AutoBRR) comes back up after a node reboot.
- Ultra.cc-compatible user systemd unit at `deploy/seedbox-torrent-manager.service`, with install/enable instructions in the README (no `screen` / `sudo` required).
- Project changelog (`CHANGELOG.md`).
- Incomplete-download reservation check: AutoBRR stop/recover and storage cleanup use **effective free** space (`quota free − Σ amount_left`), so unfinished torrents cannot overfill the quota while still downloading. Hourly Discord storage updates include reserved and effective free figures.
- Optional max torrent age cleanup (`MAX_TORRENT_AGE_ENABLED`, `MAX_TORRENT_AGE_DAYS`, default 30 days, off by default): removes completed torrents with ratio ≥ 1.0 that have been in the client too long, freeing slots for newer torrents without replacing other cleanup paths.

### Changed

- **Server mode:** failed `quota` / `app-traffic` checks no longer stop the managed app and no longer abort startup. The script keeps retrying each cycle and sends Discord alerts at most once per hour until the commands succeed again.
- Remote mode Ultra API outages still run the stop command as a precaution; recovery behaviour is unchanged.
