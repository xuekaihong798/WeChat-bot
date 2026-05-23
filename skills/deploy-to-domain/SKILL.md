---
name: deploy-to-domain
description: Deploy static sites, HTML pages, or web apps to https://097258.xyz. Use when user asks to deploy, publish, put online, make a website live, or share a link.
---

## Overview

Deploy static files or built web projects to the user's domain `097258.xyz`.

## Prerequisites

Before deploying, confirm with the user:
- What files to deploy
- If deploying a built project, run the build command first

## Deployment Steps

1. **Prepare files** — Ensure all files to deploy are in a single directory. Create an `index.html` as the entry point if one doesn't exist.

2. **Deploy via SSH** — Use `scp` or `rsync` to transfer files to the server. Ask the user for the server's SSH host and credentials the first time. Store the server info in `~/.wechat-bot/deploy.conf` for future use.

3. **Verify** — After deployment, use `curl -s -o /dev/null -w "%{http_code}" https://097258.xyz` to verify the site is accessible.

## Server Configuration Reference

The domain `097258.xyz` should be configured to serve static files from a web server (nginx, caddy, etc.). If the server isn't set up yet, guide the user through:

1. Installing nginx/caddy on the server
2. Configuring the domain's DNS A record to point to the server IP
3. Setting up HTTPS with Let's Encrypt

## Output

After successful deployment, report:
- The URL where the content is live
- The HTTP status code
- Any issues found during verification
