# Google Sign-In — local implementation and production handoff

## Existing architecture reused

No working Google handler/button existed. The new GIS popup credential is exchanged
at `POST /auth/google` for the same application JWT used by password login.
`create_access_token`, `get_current_user`, `/auth/me`, AuthContext and
`migrate_guest_data_to_user` remain the common path. AuthContext's identity change
refreshes the existing Usage/Billing providers. AuthorizationPage's `finishAuth`
restores the selected guest chart, migration status and pending question.

## Identity and security

- Official `google-auth==2.58.0` verifies Google signature, audience, issuer,
  issued-at/expiry. Required verified email, stable subject and a browser-generated
  nonce are checked before any DB work. Google's certificate request has a timeout.
- Endpoint accepts JSON only from the two current frontend origins. It does not
  accept Google's redirect/form POST flow, frontend email, or cookies as identity.
- First look up `users.google_sub`. Google email changes never silently switch the
  local account or overwrite the stored local email.
- For a first link, case-insensitive email lookup must identify exactly one user.
  Gmail or verified Workspace (`hd`) can link automatically. Other Google-verified
  addresses require the existing local password. A conflicting provider or
  ambiguous legacy email fails closed. Existing password and billing are unchanged.
- New users use the normal User defaults (Free). A cryptographically random unknown
  password hash satisfies the existing NOT NULL column; no password is disclosed.
  A Google-only user signs in with Google, not an invented default password.
- Row locks and unique subject/email constraints protect concurrent linking.
  A uniqueness race retries the complete transaction once. User/link, selected guest
  transfer and application JWT issuance commit together or roll back together.
- Only the explicitly selected guest chart is transferred. Existing ownership,
  Free/Premium limits and migration-result handling are reused unchanged.
- Google credentials and the linking password remain in component memory only.
  They are never placed in URLs or localStorage. Sensitive authApi logs were removed.
- GIS does not provide a reliable popup-close callback here. Cancellation leaves
  the ordinary auth UI available, with retry guidance; busy state starts only when
  a credential is received. Loader, verification and linking errors are recoverable.
- Missing client configuration hides the Google button and disables the endpoint.
  No fake production credential or automatic One Tap is used.

## Minimal migration

`migrations/google_sign_in.sql` adds only nullable `VARCHAR(255) google_sub` and
unique constraint `uq_users_google_sub` to `users`. Existing rows remain NULL.
Apply once before deploying backend code that selects this column. It deliberately
refuses a second application; inspect schema first. Production has NOT been changed.

## Changed files

Backend: `api/auth.py`, `services/google_identity.py`, `models/user.py`,
`database/queries.py`, `requirements.txt`, `migrations/google_sign_in.sql`,
`migrations/GOOGLE_SIGN_IN.md`, `tests/test_google_auth.py`,
`tests/test_google_postgres.py`, `.env.google.example`, `.gitignore`.

Frontend: `src/api/authApi.js`, `src/api/googleIdentity.js`,
`src/components/GoogleSignIn.js`, `src/components/GoogleSignIn.test.js`,
`src/context/AuthContext.js`, `src/pages/AuthorizationPage.jsx`,
`src/pages/AuthorizationPage.css`, `src/pages/ChartAuthFlow.test.js`,
`.env.google.example`.

## Local validation

- Full backend suite with the existing guarded localhost PostgreSQL setup:
  126/126 passed, no skips. Includes actual migration/uniqueness and concurrent
  first-login checks, plus all existing auth, chart, billing, usage and AI tests.
- Google verification tests use RSA-signed fixtures with only certificate transport
  mocked: wrong audience/issuer/signature/expiry/nonce and unverified email denied.
- Frontend: 130/130 passed (13 suites), including Google conversion with all four
  guest migration outcomes, pending question, same app JWT, and usage refresh.
- Production frontend build passed. `pip check` reports no broken dependencies.
- No real Google sign-in, production migration, commit, push or deploy performed.

## Manual Google Cloud setup

1. In Google Auth Platform select/create the project for Lunaria. Configure Branding
   as **Lunaria**, support/developer contact emails, and Audience **External** unless
   the product is intentionally restricted to your Workspace. While in Testing,
   add the Google accounts that will run the manual E2E as test users.
2. Use only basic identity data (`openid`, `email`, `profile`); no Google API access,
   offline access or refresh tokens are required. Complete any branding/domain or
   privacy-policy requirements shown by Console using real URLs, not placeholders.
3. Create an OAuth 2.0 client of type **Web application**.
4. Authorized JavaScript origins (no path or trailing slash):
   - `http://localhost`
   - `http://localhost:3000`
   - `https://astrology-ai-frontend-production.up.railway.app`
   Local auth testing should use `localhost:3000`, which is already allowed by
   backend CORS. Do not substitute 127.0.0.1 or the QA preview port without explicitly
   configuring those origins; this implementation did not expand the allowlist.
5. **Authorized redirect URIs: none.** GIS uses `ux_mode: popup` and a JavaScript
   callback. Backend URL `https://astrology-ai-backend-production.up.railway.app`
   is the API receiving JSON, not a Google redirect URI or JavaScript origin.
6. Configure the SAME public client ID in:
   - Railway backend: `GOOGLE_CLIENT_ID`
   - Railway frontend: `REACT_APP_GOOGLE_CLIENT_ID`
   - For local testing, the corresponding backend and frontend environments.
   Frontend env is bundled at build time; restart/rebuild after setting it.
   Existing API URL and other settings stay unchanged.
7. The OAuth client ID is **public**. No Google client secret is needed for this
   flow. If Console generates a client secret, do not put it in frontend, Git or
   chat. Google ID tokens, application JWTs, passwords and existing backend secret
   keys are sensitive and must not be disclosed.

Next: create this Web client and configure the two client-ID variables. Coordinate
Railway's apply/redeploy step with the later rollout: migration must precede backend
code deployment. Return with confirmation of configuration (no secrets). The next
separate phase applies the migration, deploys, and verifies real Google login,
existing-account linking, guest conversion and cancellation in the browser.

References:
- https://developers.google.com/identity/gsi/web/guides/verify-google-id-token
- https://developers.google.com/identity/gsi/web/reference/js-reference
- https://developers.google.com/identity/gsi/web/guides/get-google-api-clientid
