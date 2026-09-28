# FanEdit Metadata Provider

[![version](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Fcirculon%2Ffanedit-provider%2Fmain%2Fpyproject.toml&query=%24.project.version&label=version&color=blue)](https://github.com/circulon/fanedit-provider)
[![Docker Pulls](https://img.shields.io/docker/pulls/circulon/fanedit-provider)](https://hub.docker.com/r/circulon/fanedit-provider/tags)
[![License: MIT](https://img.shields.io/github/license/circulon/fanedit-provider)](https://github.com/circulon/fanedit-provider/blob/main/LICENSE)
[![docker-hub build status](https://img.shields.io/github/actions/workflow/status/circulon/fanedit-provider/docker_hub.yml?label=docker%20build)](https://github.com/circulon/fanedit-provider/actions/workflows/docker_hub.yml)


A Plex-compatible Custom Metadata Provider for FanEdit movies using the FanEdit.org Database (IFDB).

Based on the template 
(https://github.com/circulon/plex-metadata-provider)

Written with the assistance of Claude Code (https://www.claude.com/product/claude-code)

## Table of Contents

- [Quickstart](#quickstart)
  - [Setup container](#setup-container)
    - [Optional: Container icon](#optional-container-icon)
  - [Add a Custom FanEdit Agent to Plex](#add-a-custom-fanedit-agent-to-plex)
  - [Add or update a Library](#add-or-update-a-library)
- [Options](#options)
  - [Entry Content](#entry-content)
  - [Search](#search)
  - [FanEdit.org](#fanedit-org)
  - [Server](#server)
  - [Caching (search and entry)](#caching-search-and-entry)
- [Changes](#changes)

## Quickstart

We provide a docker image with the defaults already setup, for
`linux/amd64` and `linux/arm64` (e.g. Raspberry Pi 4/5 on a 64-bit OS, ARM
NAS devices). You can run it with docker-compose or docker.

The default port is `32900` 

### Setup container

**Using docker**

```sh
docker run -d \
    -p 32900:32900 \
    --name fanedit-provider \
    circulon/fanedit-provider
```

**Using docker compose**

```yaml
services:
    fanedit-provider:
        image: circulon/fanedit-provider
        ports:
            - "32900:32900"
        restart: unless-stopped
```

#### Optional: Container icon

The image includes an `org.opencontainers.image.icon` label pointing to a logo, for use in Docker management GUIs that support reading it (e.g. Unraid, Portainer, dashboard tools):

`https://raw.githubusercontent.com/circulon/fanedit-provider/main/provider-logo.png`

### Add a Custom FanEdit Agent to Plex

Each PMS manages it's own set of Metadata Providers and Agents.
This Provider can be used by multiple PMS instances without issue

- Open Plex settings
- Navigate to `<server name>` -> `Metadata Agents`

**Add the Provider**
- Click `Add Provider` In the `Metadata Providers` section
- Enter `http://<host_ip>:32900` in the dialog and hit `Save`
  - depending on your setup the `host_ip` maybe `127.0.0.1` or something else
- You should see `FanEdit Movies` listed in the Metadata Providers

**Add the Agent**
- On the same page scroll down to the `Metadata Agents` section
- Click `Add Agent`
- Name your agent eg `Fanedit Movies`
- Select `FanEdit Movies` as the Primary provider
- Optional but recommended additional providers 
  - Add `Plex NFO Movie` 
    - A fallback to provide extra details from `.nfo` files
  - Add `Plex Local Media` 
    - To use local assets for posters etc
- Click `Save`

### Add or update a Library 
- In the specific Library's Management dialog `Advanced` section
  - Select `FanEdit Movies` as the agent
  - Click `Save` or `Add` depending
- You may need to rescan the library or `Refresh All Metadata`

Enjoy your content!

## Options

All options are set via environment variables

### Entry Content

- `INCLUDE_EXTRA_IN_SUMMARY` : true|false - default true
  - Include `changes` in the summary 

### Search

- Matching thresholds (0-100)
  - `MINIMUM_EXACT_SCORE` minimum score for automatic (non-manual) matching.
    - a title is matched automatically only when exactly one result scores at or above this
    - if none (or more than one) do, nothing is matched
  - `MINIMUM_MANUAL_SCORE` minimum threshold for manual "fix match" searches
      - only titles scoring at or above this threshold are listed in the `match` or `fix_match` dialog

### FanEdit.org

- `FANEDIT_ORG_TIMEOUT` seconds to wait for fanedit.org to respond
  - default: 25
- `FANEDIT_ORG_CONNECT_TIMEOUT` seconds to wait for a connection (fails fast when the site is down)
  - default: 5
- `FANEDIT_ORG_MAX_CANDIDATES` maximum search results considered per search
  - default: 25
- `FANEDIT_ORG_MATCH_TYPE` fanedit.org keyword match mode: `all`, `any` or `exact`
  - default: all

### Server

- `PORT` port to listen on
  - default: 32900
- `WORKERS` number of worker processes (each keeps its own cache)
  - default: 2
- `THREADS` request threads per worker
  - default: 4

### Caching (search and entry)

Internal caching is used to reduce hits on upstream sources. The caches are per worker process.

- `ENTRY_CACHE_TTL_SECONDS` number of seconds entries are cached for
  - default: 60
- `ENTRY_CACHE_MAX_SIZE` number of entries to hold
  - default: 200
- `SEARCH_CACHE_TTL_SECONDS` number of seconds search results are cached for
  - default: 60
- `SEARCH_CACHE_MAX_SIZE` number of searches to hold
  - default: 100

## Changes

### 1.2.0
- Docker images now built for `linux/arm64` as well as `linux/amd64`
- Smaller, faster image build (lxml's wheels already include libxml2/libxslt)
- Sources, caches and services are now built once at startup, so config errors fail fast
- Removed a startup race condition when several requests arrived at once
- fanedit.org outages now return `503` (so Plex retries) instead of a cached "not found"
- Fail fast (5s connect timeout) when fanedit.org is down
- Automatic and manual matches for the same title share one cached search
- Simultaneous identical requests share one fanedit.org request
- Error responses keep their proper status (e.g. `405`, `400`) and no longer include internal error text
- Any option can be set by env var; tests no longer pick up a local `.env`
- The provider only advertises the types enabled in its config
- New options: `FANEDIT_ORG_TIMEOUT`, `FANEDIT_ORG_CONNECT_TIMEOUT`, `FANEDIT_ORG_MAX_CANDIDATES`, `FANEDIT_ORG_MATCH_TYPE`, `WORKERS`, `THREADS`
- A misconfigured source list now stops the app at startup with a clear error
- The user agent sent to fanedit.org now carries the full `major.minor` version (e.g. `FanEditProvider/1.1`)

### 1.1.0
- Fixed `changes` not always being included in the summary
- Use actual genres name instead of fanedit type
- Extract linked id when available (imdb)
- Get extra art/posters when available
- Extract critic (Trusted Reviewers) and user (Users) ratings
- Removed unnecessary env (config) var DEFAULT_PAGE_SIZE
- Increased FanEdit.org timeout
- Updated README

### 1.0.1
- Updated README

### 1.0.0
- Initial release
