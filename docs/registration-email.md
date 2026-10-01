# Registration email through Brevo SMTP

The existing `/auth/request-register` -> `/auth/verify-code` -> `/auth/login`
flow is retained. Registration creates a ten-minute six-digit verification code;
the local User is created only after successful verification. JWT, Google sign-in,
guest chart migration and existing account plans are unchanged.

The previous adapter hardcoded Zoho and defaulted to printing codes instead of
sending mail. `DEV_MODE` no longer skips email or prints codes. Local tests mock
SMTP. Missing SMTP credentials or delivery failures return a sanitized 503; the
failed request's code is removed without deleting a newer request's code. Logs
contain only the exception class, never credentials, code, recipient or raw SMTP
response. SMTP acceptance is not proof of inbox delivery: inspect Brevo's
transactional delivery events and the recipient mailbox during verification.

## Railway backend variables

| Variable | Value |
| --- | --- |
| `MAIL_SERVER` | `smtp-relay.brevo.com` |
| `MAIL_PORT` | `587` |
| `MAIL_USERNAME` | Copy the **SMTP login** from Brevo > SMTP & API > SMTP; do not substitute the sender email. |
| `MAIL_PASSWORD` | Copy the generated **SMTP key** directly into Railway. Not the Brevo account password or HTTP API key. |
| `MAIL_FROM` | `noreply@mylunariaai.com` |
| `MAIL_FROM_NAME` | `Lunaria` |

Set these on `astrology-ai-backend`, never frontend. STARTTLS and certificate
validation are always enabled; timeout is 20 seconds. Port 2525 is also accepted
for STARTTLS if needed. No database migration or new dependency is required.
Treat SMTP login/key as private configuration. Do not paste them in chat or Git.
`DEV_MODE` may be removed; it is no longer used by this email adapter.

Official references: [Brevo SMTP setup](https://help.brevo.com/hc/en-us/articles/7924908994450-Send-transactional-emails-using-Brevo-SMTP),
[Railway SMTP availability](https://docs.railway.com/networking/outbound-networking).
Railway documents SMTP for Pro and higher plans; do not assume it is available
on all plans. The current backend's unauthenticated STARTTLS connection to Brevo
was verified on ports 587 and 2525 without sending email.

## Deployment and manual verification

1. Configure the six backend variables, deploy this code, and wait for a healthy
   backend. No production delivery has been tested with the private SMTP key yet.
2. Open `https://mylunariaai.com` in a private browser window. Register a real email
   address not already associated with an account.
3. Confirm the request succeeds, the UI asks for a code, and Brevo transactional
   logs show delivery. Check Inbox and Spam. Sender must be
   `Lunaria <noreply@mylunariaai.com>`; the code has six digits and a ten-minute TTL.
4. Try a wrong code: no account/token should be granted. Submit the correct code:
   the account is created and the existing authentication flow signs in.
5. Log out and log in with the same email/password. Confirm an incorrect password
   is rejected. Reusing the consumed verification code must be rejected.
6. With another unused address, let a code expire for over ten minutes and verify
   rejection; request a fresh code and complete verification.
7. Re-registering an existing address must return the existing-account message.
   SMTP failure handling is covered with mocks; do not break production credentials
   to test failure paths.

The exact `https://mylunariaai.com` origin is allowed by backend CORS and Google
sign-in checks. For Google login on that domain, ensure the existing Google OAuth
client also lists it under Authorized JavaScript origins. No redirect URI is
required for the existing GIS popup flow. No `www` origin is added without an
explicitly configured `www` site.
