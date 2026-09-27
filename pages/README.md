# Scheduling SMS policy pages

These static pages are prepared for GitHub Pages. They identify the Small Business Scheduling Assistant proof of concept and use the supplied support email. Confirm that the statements about data use, vendors, retention, and consent match the actual service before publishing. The pages intentionally do not name the person behind the registered Twilio brand; that may cause a brand mismatch during A2P review.

The site source is in `/pages` on branch `codex/a2p-policy-pages`. GitHub's branch publishing setting cannot use `/pages`, so `.github/workflows/deploy-pages.yml` uploads this folder. In **Settings → Pages**, choose **GitHub Actions** as the source. Then run the **Deploy policy pages** workflow from the **Actions** tab if it has not already run successfully. The expected direct links are:

- `https://ea-ai-projects.github.io/small-business-scheduling-assistant/privacy-policy/`
- `https://ea-ai-projects.github.io/small-business-scheduling-assistant/terms-and-conditions/`

Check both public URLs in a signed-out browser before entering them in Twilio. Publishing from GitHub Pages requires repository admin or maintainer access. GitHub Free requires a public repository for Pages; the repository was not publicly accessible when checked signed out. Do not change its visibility without reviewing what source code would become public.

These pages are only one part of A2P registration. The actual point of opt-in must separately identify the sender, message type, frequency, potential message/data charges, STOP instructions, and direct links to these pages. The Twilio `message_flow` must describe each real opt-in method and use the same links. Do not describe a consent method that is not in use. Set up and test STOP and HELP handling before production messaging. If a real cleaning business becomes the sender, create policies and a campaign that identify that business before messaging its clients.
