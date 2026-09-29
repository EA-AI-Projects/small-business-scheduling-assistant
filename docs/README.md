# Scheduling SMS policy site

These static pages are prepared for GitHub Pages. They identify the Small Business Scheduling Assistant proof of concept and use the supplied support email. Confirm that the statements about data use, vendors, retention, and consent match the actual service before publishing. The pages intentionally do not name the person behind the registered Twilio brand; that may cause a brand mismatch during A2P review.

GitHub Pages publishes these files from branch `main`, folder **/docs** (**Settings → Pages → Deploy from a branch**). Enrique authorized publication of these pages. Changes merged to `main` under `docs/` go live automatically. The expected direct links are:

- `https://ea-ai-projects.github.io/small-business-scheduling-assistant/privacy-policy/`
- `https://ea-ai-projects.github.io/small-business-scheduling-assistant/terms-and-conditions/`
- `https://ea-ai-projects.github.io/small-business-scheduling-assistant/sms-consent/` (public in-person consent script and process)

Check both public URLs in a signed-out browser before entering them in Twilio. Publishing from GitHub Pages requires repository admin or maintainer access. GitHub Free requires a public repository for Pages; this repository is public.

These pages are only one part of A2P registration. The actual point of opt-in must separately identify the sender, message type, frequency, potential message/data charges, STOP instructions, and direct links to these pages. The Twilio `message_flow` must describe each real opt-in method and use the same links. Do not describe a consent method that is not in use. Set up and test STOP and HELP handling before production messaging. If a real cleaning business becomes the sender, create policies and a campaign that identify that business before messaging its clients.

The public `sms-consent/` page describes an in-person opt-in with a private consent log. Only submit that flow to Twilio if it is the process actually used for every test participant. The program currently has no SMS keyword or website opt-in flow.
