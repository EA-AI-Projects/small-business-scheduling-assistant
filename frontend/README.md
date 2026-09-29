# Owner web app

Static Next.js (Pages Router) + React + TypeScript app for the owner calendar, built with `output: "export"` and hosted on AWS Amplify Hosting. It has no SSR, server actions, or API routes; all data comes from the authenticated owner API, which allows CORS only from the configured app origin.

## Local development

Requires Node 22 (see `.nvmrc`).

1. Start the synthetic, in-memory owner API from the repository root:

   ```sh
   export LOCAL_OWNER_TOKEN="$(openssl rand -hex 16)"; echo "$LOCAL_OWNER_TOKEN"
   backend/.venv/bin/uvicorn scheduling.local_owner:app --app-dir backend --host 127.0.0.1 --port 8000
   ```

2. In `frontend/`:

   ```sh
   cp .env.example .env.local   # local mode, API at http://127.0.0.1:8000
   npm ci
   npm run dev                  # http://127.0.0.1:3000
   ```

3. Paste the token into the local sign-in form. It is kept in page memory only.

The same backend serves a text simulator at `http://127.0.0.1:8000/local/texts` that shares this calendar. Unless `OPENAI_API_KEY` is set in your shell, it accepts only exact commands; see [Try the app locally](../README.md#try-the-app-locally).

Local mode exists only for the synthetic local API. Amplify builds use Cognito mode.

## Configuration

All settings are public `NEXT_PUBLIC_*` build variables that ship in the bundle. Never put a secret, owner subject, or customer data in them.

| Variable | Cognito (Amplify) | Local |
| --- | --- | --- |
| `NEXT_PUBLIC_AUTH_MODE` | `cognito` (default) | `local` |
| `NEXT_PUBLIC_API_BASE_URL` | Owner HTTP API URL, HTTPS, no trailing slash | `http://127.0.0.1:8000` |
| `NEXT_PUBLIC_BUSINESS_ID` | Pilot business ID | `pilot` |
| `NEXT_PUBLIC_COGNITO_DOMAIN` | Hosted UI origin, e.g. `https://<prefix>.auth.us-west-1.amazoncognito.com` | unused |
| `NEXT_PUBLIC_COGNITO_CLIENT_ID` | Public app client ID | unused |

The Cognito callback and sign-out URL is the app origin followed by `/` (the stack's `OwnerAppOrigin` parameter plus `/`).

## Security model

- Sign-in uses the Cognito hosted UI authorization-code flow with PKCE (`openid` scope). Only the one-time PKCE verifier and OAuth state are held in `sessionStorage`, and only across the redirect.
- The access token is held in React state only. It is never written to storage, cookies, or the URL, and it is sent only to `NEXT_PUBLIC_API_BASE_URL`. Reloading the page requires signing in again. Sign-out also ends the hosted UI session.
- `_document.tsx` renders a build-time Content-Security-Policy `<meta>` tag that allows scripts and styles only from the app itself and connections only to the API and Cognito origins. The repository-root `customHttp.yml` adds `frame-ancestors 'none'`, HSTS, `nosniff`, and `no-referrer` response headers on Amplify. `npm run check:export` fails the build if the export contains inline scripts or styles.

## Checks

```sh
npm run lint && npm run typecheck && npm test && npm run build && npm run check:export
```

`src/api/schema.d.ts` is generated from the backend's owner OpenAPI schema:

```sh
backend/.venv/bin/python -m scheduling.owner_openapi > frontend/openapi/owner.json   # from repo root
npm run generate:api                                                                  # in frontend/
```

CI fails if either generated file is stale. The owner routes do not declare response models, so response types are maintained by hand in `src/api/types.ts`.

## Amplify Hosting

`amplify.yml` at the repository root builds this directory as the monorepo app root. `customHttp.yml`, also at the root, uses the monorepo `applications`/`appRoot` format; `src/hosting.test.ts` checks both files. Create the Amplify app with platform `WEB` (static hosting). Amplify may detect Next.js and default to `WEB_COMPUTE`, which expects a server build rather than `out/`. On the Amplify app, set `AMPLIFY_MONOREPO_APP_ROOT=frontend` and the Cognito-mode variables above. Creating the Amplify app or connecting the repository is a deployment step that needs separate authorization (#43).
