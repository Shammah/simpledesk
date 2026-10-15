# Putting SimpleDesk online (DigitalOcean)

This guide takes you from this computer to a live, secure website, then to the
Play Store, and lists what to do to keep it healthy. Commands in grey boxes are
typed into PowerShell on this computer unless it says "on the server".

## 1. Create the server (about 10 minutes)

1. Make an SSH key on this computer (skip if you have one). Press Enter at each question:

   ```powershell
   ssh-keygen -t ed25519
   ```

   Then show the public half, to paste into DigitalOcean:

   ```powershell
   Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub
   ```

2. Sign up at digitalocean.com and choose **Create > Droplets**:
   - **Region:** the one nearest your customers. For East Africa, Frankfurt or London.
   - **Image:** Ubuntu 24.04 (LTS).
   - **Size:** Basic, Regular, 1 GB RAM (about $6 a month) is plenty to start.
   - **Authentication:** SSH Key, and paste the key from step 1.
   - **Backups:** turn on. DigitalOcean then keeps whole-server copies too
     (about 20% of the server price). This is your safety net if the server is lost.
3. Create it and note its **IP address**, for example `203.0.113.5`.

Check you can sign in (type `yes` the first time, then `exit`):

```powershell
ssh root@203.0.113.5
```

## 2. Put the app on it (one command)

From the SimpleDesk folder on this computer:

```powershell
.\deploy\upload.ps1 -Server root@203.0.113.5 -Setup
```

This installs everything, turns on the firewall and HTTPS, and starts the app,
the mail checker and the nightly backup. At the end it prints your address.
Without a domain it uses a free one made from your IP, for example
`https://203-0-113-5.sslip.io`. Open it and create your account.

Then set the contact email shown on the privacy policy (on the server):

```bash
nano /etc/simpledesk.env
systemctl restart simpledesk simpledesk-mail
```

Email and WhatsApp settings live on the server at `/var/lib/simpledesk/channels.ini`
(same format as `channels.example.ini`). Edit it with `nano` the same way.

**Moving your existing account from this computer (optional).** Your local data
is never uploaded automatically. To copy it across once, before anyone uses the
server version:

```powershell
scp data\helpdesk.db root@203.0.113.5:/var/lib/simpledesk/helpdesk.db
ssh root@203.0.113.5 "chown simpledesk:simpledesk /var/lib/simpledesk/helpdesk.db && rm -f /var/lib/simpledesk/helpdesk.db-wal /var/lib/simpledesk/helpdesk.db-shm && systemctl restart simpledesk simpledesk-mail"
```

## 3. Get your own domain (do this before the Play Store)

The free sslip.io address is fine for testing, but the Android app is tied to
the web address it was built for. Changing address later means rebuilding the
app, so get a domain first.

1. Buy one (about $10–15 a year) from a registrar such as Cloudflare, Namecheap
   or Porkbun. DigitalOcean does not sell domains.
2. In the registrar's DNS settings add an **A record**: name `help` (or `@` for
   the bare domain), value = your server's IP.
3. Wait until it works (usually minutes), then point SimpleDesk at it:

   ```powershell
   .\deploy\upload.ps1 -Server root@203.0.113.5 -Setup -Domain help.yourdomain.com
   ```

4. On the server, set `public_url = https://help.yourdomain.com` in
   `/var/lib/simpledesk/channels.ini`.

## 4. Publish on Google Play

1. **Developer account:** play.google.com/console, one-time $25 fee and ID
   checks. New personal accounts currently must run a closed test with at
   least 12 testers for 14 days before going public. An organisation account
   (needs a free D-U-N-S number) avoids this.
2. **Build the Android app:** go to pwabuilder.com, enter
   `https://help.yourdomain.com`, choose **Package for stores > Android**, then
   in the options:
   - **Package ID:** for example `com.yourbusiness.simpledesk` (it can never change).
   - **Start URL:** `/?source=android-app` (this is what hides plan payments in the app).
   - **Signing key:** create new.

   Download the zip. **Back up the signing key file and its passwords somewhere
   safe.** You need them for every future update of the Android app.
3. **Link the app to your website:** the download includes `assetlinks.json`.
   Copy the package name and the `sha256_cert_fingerprints` value into
   `/etc/simpledesk.env` on the server:

   ```bash
   SIMPLEDESK_ANDROID_PACKAGE=com.yourbusiness.simpledesk
   SIMPLEDESK_ANDROID_SHA256=AA:BB:CC:...
   ```

   After uploading to Play, Google re-signs the app. Copy the extra fingerprint
   from Play Console > your app > **App integrity** and add it after a comma.
   Restart with `systemctl restart simpledesk`, then check that
   `https://help.yourdomain.com/.well-known/assetlinks.json` shows both.
   Without this, the app shows a browser bar at the top.
4. **Store listing:** upload the `.aab` file from PWABuilder. You will need:
   - Privacy policy: `https://help.yourdomain.com/privacy-policy`
   - Icon: `static/icons/icon-512.png`
   - Feature banner 1024×500 and at least 2 phone screenshots
   - The Data safety form: the app collects names, emails, phone numbers and
     messages, for app functionality, encrypted in transit, not sold, and
     users can ask for deletion (matching the privacy policy).

Inside the Android app, prices and plan changes are hidden automatically
(Google requires its own billing for subscriptions sold in an app). Customers
manage plans on the website.

## 5. Updating the app

Change things on this computer, test them, then:

```powershell
.\deploy\upload.ps1 -Server root@203.0.113.5
```

It backs up first, installs the new version, checks it starts, and goes back
to the previous version by itself if it does not. Android users get the change
straight away; the Play Store is not involved.

You only rebuild the Android app (PWABuilder, same signing key, higher version
number) to change its name or icon, or once a year when Google asks apps to
target a newer Android version.

## 6. Backups

- **Every night at 02:30 (server time)** a checked copy of the database and
  settings is saved to `/var/backups/simpledesk`. 30 days are kept.
- **Weekly,** copy them to this computer, so a copy exists outside DigitalOcean:

  ```powershell
  .\deploy\download-backups.ps1 -Server root@203.0.113.5
  ```

- **DigitalOcean Backups** (step 1) keep whole-server copies as well.

Make a backup by hand (on the server): `systemctl start simpledesk-backup`

**Restoring** (on the server): pick a backup zip, then:

```bash
systemctl stop simpledesk simpledesk-mail
cd /tmp && unzip -o /var/backups/simpledesk/simpledesk-backup-YYYY-MM-DD-HHMM.zip -d restore
cp restore/helpdesk.db restore/channels.ini restore/secret.key /var/lib/simpledesk/
rm -f /var/lib/simpledesk/helpdesk.db-wal /var/lib/simpledesk/helpdesk.db-shm
chown simpledesk:simpledesk /var/lib/simpledesk/*
systemctl start simpledesk simpledesk-mail
```

(`apt install unzip` first if needed.) Try a restore once early on, so you
know it works before you need it.

## 7. Keeping it healthy

**Set up once: monitoring (both free)**

*UptimeRobot: tells you when the site is down.*
1. Sign up at uptimerobot.com, then **New monitor**.
2. Type **HTTP(s) - Keyword**. URL `https://your-address/healthz`, keyword `ok`
   (alert when the keyword does *not* exist), interval 5 minutes.
3. Add your email, and the UptimeRobot phone app for push alerts.

`/healthz` answers `ok` only when the website, the database and (once email
is switched on) the mailbox checker are all working. Otherwise UptimeRobot alerts you.

*Sentry: tells you when something breaks inside the app, and if a nightly backup fails or doesn't run.*
1. Sign up at sentry.io, create a project of type **Flask**, and copy its **DSN**
   (Project settings > Client Keys).
2. On the server: `nano /etc/simpledesk.env`, add the line `SENTRY_DSN=<your DSN>`, save, then
   `systemctl restart simpledesk simpledesk-mail`.
3. The "nightly-backup" monitor appears under **Crons** after the first backup
   (or run one now: `systemctl start simpledesk-backup.service`).

Sentry only gets technical details. Cookies, form contents and web addresses
with private links are left out, and email addresses, phone numbers and passwords
are blanked before anything is sent.

**Weekly**
- Download the backups (section 6).
- Glance at the Channels page for messages that could not be delivered.

**Monthly**
- Update the Python packages: on this computer run
  `.venv\Scripts\python.exe -m pip install -U -r requirements.txt`, test, then upload.
- Restart the server once for system updates (they install themselves; some need a restart):
  `ssh root@203.0.113.5 reboot`

**Yearly**
- Rebuild the Android app when Google asks (Play Console emails you).
- Renew the domain (turn on auto-renew).
- Review the privacy policy date and wording.

## Useful commands (on the server)

| What | Command |
|---|---|
| Is everything running? | `systemctl status simpledesk simpledesk-mail caddy` |
| Recent app messages | `journalctl -u simpledesk -n 50` |
| Recent mailbox messages | `journalctl -u simpledesk-mail -n 50` |
| Restart after changing settings | `systemctl restart simpledesk simpledesk-mail` |
| When is the next backup? | `systemctl list-timers simpledesk-backup` |
| Disk space | `df -h /` |
