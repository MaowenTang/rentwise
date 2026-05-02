# RentWise v0 — Deployment Guide

Backend → **Railway** (FastAPI). Frontend → **Vercel** (Next.js).
Both have free trials; total monthly cost for testing-scale usage: ~$0–5.

## Prerequisites (one-time)

1. **Anthropic API key** — already have one in `api/.env`. Copy the value; you'll paste it into Railway's env vars.
2. **GitHub account** — needed if you want auto-redeploy on push (recommended).
3. **Railway account** — sign up at https://railway.com (use GitHub login, $5 trial credit auto-applied).
4. **Vercel account** — sign up at https://vercel.com (use GitHub login, free tier is generous).

---

## Path A — GitHub-driven (recommended, ~30 min)

Best because every `git push` auto-redeploys both services.

### Step 1. Push the repo to GitHub
```bash
cd ~/Downloads/rentwise
gh repo create rentwise --private --source=. --remote=origin --push
# If you don't have gh CLI, do it in the browser:
# 1) github.com/new → name it "rentwise" → Private → Create
# 2) git remote add origin https://github.com/<you>/rentwise.git
# 3) git push -u origin main
```

### Step 2. Deploy backend to Railway
1. railway.com → **New Project** → **Deploy from GitHub repo** → pick `rentwise`.
2. Railway auto-detects Python via `nixpacks.toml`. Once the build starts:
3. **Settings → Root Directory** → set to `api`
4. **Variables** → add:
   ```
   ANTHROPIC_API_KEY=sk-ant-api03-...   (paste your key)
   ```
5. **Settings → Networking → Generate Domain** → click **Generate Domain**. You get something like `rentwise-api-production.up.railway.app`.
6. Wait for the deploy to go green (first build ~3 min — installs Python deps and uvicorn). Verify:
   ```bash
   curl https://rentwise-api-production.up.railway.app/healthz
   # → {"ok":true,"listings_loaded":1087,"anthropic_key_present":true,"agents":[...]}
   ```

### Step 3. Deploy frontend to Vercel
1. vercel.com/new → **Import Git Repository** → pick `rentwise`.
2. **Root Directory** → set to `web`.
3. Framework preset → **Next.js** (auto-detected).
4. **Environment Variables** → add:
   ```
   NEXT_PUBLIC_API_URL = https://rentwise-api-production.up.railway.app
   ```
   (use the Railway URL from step 2.5)
5. Click **Deploy**. First build takes ~2 min.
6. You get a URL like `rentwise.vercel.app`. Open it in a browser — should show the dark/light chat with the 4 agents.

### Step 4. Tell Railway about the Vercel domain (CORS)
1. Back in Railway → **Variables** → add:
   ```
   CORS_ORIGINS = https://rentwise.vercel.app
   ```
   (Optional — `*.vercel.app` is already allowed via regex, so this is a belt-and-suspenders.)
2. Railway redeploys automatically.

### Done. Share `https://rentwise.vercel.app` with friends.

---

## Path B — CLI-only (no GitHub, ~20 min)

Faster if you don't want a GitHub repo.

### Backend (Railway CLI)
```bash
brew install railway   # macOS — or grab installer from railway.com/cli
cd ~/Downloads/rentwise/api
railway login          # opens browser, click confirm
railway init           # name it "rentwise-api"
railway variables --set ANTHROPIC_API_KEY=sk-ant-api03-... 
railway up             # builds + deploys (~3 min)
railway domain         # generates a public URL
```

### Frontend (Vercel CLI)
```bash
npm install -g vercel
cd ~/Downloads/rentwise/web
vercel login           # opens browser
vercel --prod          # answers: project name "rentwise", root "."
                       # add NEXT_PUBLIC_API_URL when prompted
                       # → outputs https://rentwise.vercel.app
```

If `vercel` doesn't prompt for env vars, add them after:
```bash
vercel env add NEXT_PUBLIC_API_URL production
# paste the Railway URL when prompted
vercel --prod          # redeploy with the env var
```

---

## Costs (free tier reality check)

| Service | Free tier | Likely monthly cost at testing-scale |
|---|---|---|
| Railway | $5 trial credit (one-time), then $5/mo minimum | $0 first month, ~$5/mo after |
| Vercel | Hobby tier: 100 GB bandwidth, unlimited deploys | $0 |
| Anthropic API | Pay-per-use (~$0.005–0.02 per chat turn) | $1–10 for casual testing |

Total: **$1–15/mo** for friend-testing-scale usage. If Railway's $5 minimum bothers you, swap to Render (sleeps after 15 min idle but free) or Fly.io (3 small VMs free, no sleep).

---

## Things to watch for once it's live

- **Anthropic spend**: every chat turn = 2–5 LLM calls. If a teammate sends 50 messages, that's ~$0.50. Set a usage alert at console.anthropic.com.
- **CORS errors in browser**: usually mean Railway's `CORS_ORIGINS` doesn't include your Vercel URL. Quick check: open DevTools → Network → look for the `/chat` request → if blocked with CORS, fix in Railway env vars.
- **Cold starts on free tier**: if you swap to Render, the first request after 15min idle takes ~30s. Railway and Fly don't sleep.
- **Logs**:
  - Backend: `railway logs` (CLI) or Railway dashboard → Deployments → click the active deploy
  - Frontend: Vercel dashboard → Deployments → Functions tab

---

## Updating later

With Path A (GitHub-connected), every push to `main` auto-redeploys both:
```bash
cd ~/Downloads/rentwise
git add .
git commit -m "fix: tweak ranking weights"
git push
# Railway and Vercel both pick it up automatically
```

With Path B (CLI), you manually re-run `railway up` and `vercel --prod`.
