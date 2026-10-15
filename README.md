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

## Getting paid (Paystack: M-Pesa and cards)

IT providers start on the free Starter plan (1 technician) and pay per
technician for the Team plan, in Kenyan shillings. Prices are in `config.py`.

1. Create an account at paystack.com (Kenya) and complete business verification.
2. Under Settings > API Keys & Webhooks, copy the **secret key** into
   `[paystack]` in `data/channels.ini`. Use the `sk_test_` key first; Paystack's
   test M-Pesa numbers and cards let you try the whole flow without real money.
3. On the same Paystack page, set the **Webhook URL** to the address shown on the
   Channels page (`.../webhooks/paystack`).

Owners pay for 1, 3 or 12 months with M-Pesa or a card on Paystack's page. Card
payments renew automatically; M-Pesa payers get a reminder 3 days before the
paid time ends. If nobody pays, the Team plan keeps working for 7 more days,
then moves to Starter. Nothing is deleted. While no key is set, the Team plan
can be switched on for free, for testing.

Inside the Android app, prices and payment are hidden (Google's rules); owners
pay on the website.

## Other everyday features

- **Forgot password / invites:** technicians reset their own password by email,
  and new technicians get an invite link to choose their own. If email isn't
  set up, the owner gets the link to pass on. `tools/reset_link.py EMAIL` makes
  one from the command line, for when even the owner is locked out.
- **Alerts:** technicians get an email when a ticket needs them, when staff
  reply, and when a reply is overdue. Each chooses all tickets, only their own,
  or none (Team & plan page).
- **Priority and response targets:** urgent 1 hour, high 4, normal 8, low 24
  (clock hours, in `config.py`). Staff choose how much it stops their work.
- **Pictures and PDFs** on the help page, email and WhatsApp, up to 5 MB each.
- **Time tracking and monthly client reports:** log minutes on each ticket; the
  report (print, save as PDF or email) shows requests, response times, ratings,
  time against the hours in their contract, and warranties ending. It can be
  emailed automatically on the 1st of each month.
- **Search** (top of the menu; `#12` jumps to ticket 12), **saved replies**,
  and a **satisfaction rating** when a request is closed.
- **Account** (click your name in the menu): profile, photo, password, email
  alerts, your month's numbers, and "sign out all other devices".
- **Settings:** business name, billing email, the support phone and hours shown
  on clients' help pages, theme/sound/confetti for this device, your data
  download, and deleting the business account.
- **About us** (`/about`, public): write your own story in `ABOUT_STORY` in
  `config.py`; it stays hidden until you do.

Reminders, renewals, overdue alerts and automatic reports run in the
background every 15 minutes: inside `python app.py` on this computer, and in
the mail service on the server.

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
| `payments.py` | Paystack: M-Pesa and card payments. |
| `files.py` | Pictures and PDFs attached to tickets (kept in `data/uploads`). |
| `monitoring.py` | Sentry error alerts and the health check. |
| `tools/reset_link.py` | One-time password link for a locked-out account. |
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
| `data/channels.ini` | Mail, WhatsApp and Paystack keys. Keep it private. |

## Before selling it to real customers

- Put it online with HTTPS by following [deploy/DEPLOY.md](deploy/DEPLOY.md).
  `python app.py` is for trying it out on this computer.
- Add your Paystack live key (see "Getting paid") and test one real payment.
- Add sign-in attempt limits and a limit on requests per help link.
- One mailbox and one WhatsApp number serve the whole installation. Give each
  IT provider their own before selling to other providers.
- For WhatsApp replies after the 24-hour window, add approved message templates.
- Move from SQLite to PostgreSQL once several technicians work at the same time.
- Have a lawyer review a privacy policy for the countries you sell in.
