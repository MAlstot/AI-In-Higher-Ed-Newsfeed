# Daily AI Newsletter Automation

GitHub Actions replaces the manual Feedly + Claude + Outlook workflow.

## Secrets to add (Settings → Secrets and variables → Actions)
- `ANTHROPIC_API_KEY` — from https://console.anthropic.com
- `RECIPIENT_EMAIL` — where the newsletter is sent
- `SMTP_HOST` — e.g. `smtp.gmail.com` or `smtp.office365.com`
- `SMTP_PORT` — `587` (STARTTLS) or `465` (SSL)
- `SMTP_USER` — SMTP login (usually the sending address)
- `SMTP_PASSWORD` — app password, NOT your account password

Gmail app passwords: https://myaccount.google.com/apppasswords (2FA required).

## Running manually
Actions tab → Daily AI Newsletter → Run workflow.
