# SimpleDesk

A helpdesk for IT providers who look after several client companies. Staff ask
for help by a help link, email or WhatsApp, get an instant answer when a guide
covers it, and reach a technician when it does not. Every request is tied to a
person and their device.

## Run it

```bash
.venv\Scripts\python.exe app.py
```

Then open http://127.0.0.1:5000 and create an account.

On a new computer, set up first with `python -m venv .venv` and
`.venv\Scripts\python.exe -m pip install -r requirements.txt`.

The app starts empty: no guides and no checklists. Write your own under
**Guides** and **Checklists**, or build guides up as you go by ticking "save
this reply as a guide" when you resolve a ticket.

**Putting it online, the Play Store, backups and maintenance:** see
[deploy/DEPLOY.md](deploy/DEPLOY.md).

## Backups on this computer

```bash
.venv\Scripts\python.exe tools\backup.py
```

Saves a checked copy of the database and settings to `data\backups` and
removes copies older than 30 days. On the server this runs every night by
itself.

## How a request flows

1. A staff member describes the problem by help link, email or WhatsApp.
   Passwords and card-like numbers are removed before anything is saved.
2. Fixed rules check the request. Security issues, anything needing
   administrator rights, data-loss risks and money matters go straight to a
   technician.
3. Otherwise they get an automatic answer, using only shared guides and that
   company's guides. The answer is checked again before it is sent.
4. They say whether it worked: a button on the help page, FIXED or HELP by
   email, 1 or 2 on WhatsApp.
5. When a technician resolves a ticket they can save the fix as a guide, so it
   is answered automatically next time.

## Email and WhatsApp

Copy `channels.example.ini` to `data/channels.ini` and fill in the sections you
want. The **Channels** page in the app shows what is on and lists any message
that could not be delivered.

### Email

- `[email_out]` is your mail provider's SMTP details. Gmail and Microsoft 365
  need an app password.
- `[email_in]` is a mailbox used only for the helpdesk, read over IMAP once a
  minute. Every unread email in it becomes a request and is marked read, so do
  not point it at a personal inbox. Restart the app after first turning it on.
- A sender is matched to a company by their address under **People**, or by
  the company's **email domain** on the company page. Unknown senders get one
  polite reply and no ticket.

### WhatsApp (Meta Cloud API)

You need a Meta Business account and a phone number not already used on
WhatsApp.

1. At developers.facebook.com create an app and add the WhatsApp product.
2. Fill in `[whatsapp]` in `data/channels.ini` from the API Setup page. Use a
   permanent System User token; the temporary token expires in 24 hours.
3. Host the app on a public HTTPS address and set `public_url` under `[app]`.
4. In Meta's webhook settings enter the Callback URL shown on the Channels
   page and your `verify_token`, then subscribe to the `messages` field.
5. Add each staff member's WhatsApp number, with country code, under People.

Limits to know: Meta only lets a business reply freely within 24 hours of the
person's last message, and replies are plain text.

## Installing on a home screen

SimpleDesk can be installed like an app, with its own icon and window.

- **Chrome or Edge (Windows, Mac, Android):** an **Install app** button appears
  in the menu, or use the install icon in the address bar.
- **iPhone and iPad (Safari):** Share button, then **Add to Home Screen**.

Technicians get SimpleDesk. A client's staff who install from their company's
help link get an "IT help" icon that opens straight onto that help page.

Installing only works over HTTPS (or on this computer at
http://localhost:5000), so other phones can install it once the app is hosted.
When offline, the installed app shows a "You are offline" page; no tickets or
client details are stored on the device.

The icon pictures are in `static/icons`. If you change the logo, edit
`static/favicon.svg` and redraw the pictures with
`.venv\Scripts\python.exe tools\make_icons.py` (needs `pip install pillow`).

## Turning on AI-written answers

Without an AI key, automatic answers come from your written guides only. To
have Claude write the answers, set an Anthropic API key before starting the app:

```bash
$env:ANTHROPIC_API_KEY = "your key"
```

Keys come from https://platform.claude.com. The model is set in `config.py`.

## Where things are

| File | What it does |
|---|---|
| `config.py` | Product name, prices, plan limits, AI limits. Start here. |
| `app.py` | All screens and actions. |
| `ai.py` | Automatic answers and their safety rules. |
| `channels.py` | Sending and receiving email and WhatsApp. |
| `db.py` | Database tables. |
| `channels.example.ini` | Template for `data/channels.ini`. |
| `templates/`, `static/` | Page layouts and styling. |
| `static/manifest.webmanifest`, `static/sw.js` | What lets it install on a home screen. |
| `tools/make_icons.py` | Redraws the home-screen icons. |
| `tools/backup.py` | Daily backup. |
| `tools/email_worker.py` | Checks the mailbox (runs as its own service on the server). |
| `deploy/` | Server setup, updates and the deployment guide. |
| `templates/privacy_policy.html` | The public privacy policy. |
| `data/helpdesk.db` | The database. Back this file up. |
| `data/channels.ini` | Mail and WhatsApp passwords. Keep it private. |

## Before selling it to real customers

- Put it online with HTTPS by following [deploy/DEPLOY.md](deploy/DEPLOY.md).
  `python app.py` is for trying it out on this computer.
- Connect payments. Plans and per-technician pricing are calculated, but no
  money is collected yet.
- Let technicians reset their own passwords by email.
- Add sign-in attempt limits and a limit on requests per help link.
- One mailbox and one WhatsApp number serve the whole installation. Give each
  IT provider their own before selling to other providers.
- For WhatsApp replies after the 24-hour window, add approved message templates.
- Move from SQLite to PostgreSQL once several technicians work at the same time.
- Have a lawyer review a privacy policy for the countries you sell in.
