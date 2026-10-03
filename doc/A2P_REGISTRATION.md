# A2P registration notes for the scheduling pilot

The public policy pages are in `docs/`. They describe the Small Business Scheduling Assistant proof of concept and contain no placeholder brand or contact details. The stated support email is `ealeman.kikito@gmail.com`. Review the policies against actual behavior before publishing or submitting them to Twilio.

Status (issue #91): the Twilio account is upgraded, a US number is bought, and the A2P 10DLC brand and campaign are approved; the approved brand is a Sole proprietor brand in the owner's individual name with verified identity (created 2026-09-27), which is fine for dev and pilot testing, and a business sender is needed before a real business uses the program in production (owner decision, #91); the owner verified that API sending works. Approval does not authorize live SMS, deployment, or real clients. Until launch approval, send only to explicitly authorized test numbers.

## Sender and consent

The registered Twilio A2P brand is a Sole proprietor brand in the owner's individual name, while these public pages identify only the proof-of-concept program. Twilio may reject a campaign if the sender identified in the policies, consent request, and sample messages cannot be connected clearly to the registered brand. Do not assert that a fictional cleaning business is a real sender. Before production use by a real business, register the appropriate business sender and align all consumer-facing disclosures with it. Twilio handles subscribe/unsubscribe messages (opt-in keywords and auto-reply, STOP, HELP) automatically; the app changes nothing about them and sends no custom STOP/HELP replies. The app's only message on consent is the welcome text below (owner decision, 2026-10-03, #192). The owner reports (2026-10-03, #182) that the only approved campaign message he sees in the Twilio console is the opt-in message, which has no sender name (see "Suggested confirmation text").

The planned opt-in is in person before a participant is enrolled in the app. The operator should show the [public consent process](https://ea-ai-projects.github.io/small-business-scheduling-assistant/sms-consent/), read the exact script below, ask for a clear yes/no response, and retain a private record of consent (participant name and number, date/time, in-person method, yes response, and script version). A refusal must not prevent someone from obtaining the underlying service. Do not send an initial automated enrollment text to obtain consent from someone who has not opted in.

In-person disclosure, matching the public page; use this exact script if it is the flow submitted to Twilio:

> Would you like to receive text messages from the Small Business Scheduling Assistant proof of concept about test appointment scheduling, including appointment requests, confirmations, changes, and cancellations? Message frequency varies with your scheduling activity and replies. Message and data rates may apply. Reply HELP for help or STOP to opt out at any time. Our terms are at https://ea-ai-projects.github.io/small-business-scheduling-assistant/terms-and-conditions/ and our privacy policy is at https://ea-ai-projects.github.io/small-business-scheduling-assistant/privacy-policy/. Text consent is optional and is not required to receive any underlying service. Do you agree to receive these texts? Please answer yes or no.

The Twilio `message_flow` must describe the in-person conversation, the exact disclosure, how a clear yes is recorded, and what confirmation follows. If any other opt-in method is actually used, describe it too. Verbal consent is not sufficient for marketing texts; this program is drafted for scheduling messages only.

## Correction for rejection 30896

This section is submission history; the campaign was later approved (issue #91). It does not record which `message_flow` text was approved.

The rejected `message_flow` said only: “Small business owner asks user for consent personally before enrolling them on the service.” That did not show the consent language, where it was delivered, how the yes was recorded, or public evidence. The screenshot also showed YES and SUBSCRIBE as opt-in keywords, although the described process did not use text-to-join.

After the in-person process above is actually in use and the public script URL works, replace the `message_flow` with this text (under Twilio's 2049-character limit):

> Test participants opt in in person before their phone number is enrolled in the Small Business Scheduling Assistant proof of concept. The operator shows the participant https://ea-ai-projects.github.io/small-business-scheduling-assistant/sms-consent/ and reads the exact script shown there: “Would you like to receive text messages from the Small Business Scheduling Assistant proof of concept about test appointment scheduling, including appointment requests, confirmations, changes, and cancellations? Message frequency varies with your scheduling activity and replies. Message and data rates may apply. Reply HELP for help or STOP to opt out at any time. Our terms are at https://ea-ai-projects.github.io/small-business-scheduling-assistant/terms-and-conditions/ and our privacy policy is at https://ea-ai-projects.github.io/small-business-scheduling-assistant/privacy-policy/. Text consent is optional and is not required to receive any underlying service. Do you agree to receive these texts? Please answer yes or no.” Only a clear verbal yes results in enrollment. The operator privately records the participant's name and phone number, date/time, in-person method, yes response, and script version. A no results in no enrollment or texts. After approval and consent, the first program text confirms enrollment with message frequency, rates, HELP, and STOP. This is scheduling-only, with no marketing or website/keyword opt-in.

Campaign **Opt-in Keywords** and **Opt-in Message**: an earlier recommendation to blank them is superseded by the owner decision (2026-10-03, #192). Nothing changes; Twilio manages them, along with STOP/HELP. See Twilio's [registration quickstart](https://www.twilio.com/docs/messaging/compliance/a2p-10dlc/quickstart) for the distinction between keyword and non-keyword opt-in fields.

Suggested confirmation text after in-person consent and campaign approval:

> Smart Scheduling Assistant: You're enrolled for appointment scheduling texts. Message frequency varies. Message and data rates may apply. Reply HELP for help or STOP to opt out.

This is the welcome text implemented by #182. It is separate from the campaign's Opt-in Message, and the owner keeps this wording as written (owner decision, 2026-10-03, #192). The campaign's Opt-in Message is the auto-reply to its opt-in keywords, which stays as Twilio manages it; as reported by the owner (2026-10-03, #182), it has no sender name and reads: "You are now Opted-in, please reply with Help to show help options, ask to opt-out and say: STOP to opt-out". The earlier open questions about blanking those keywords and the sender name for STOP/HELP replies are resolved by the #192 decision: no change.

## Before submitting

The campaign is approved (issue #91), so this checklist is history, except that items 1 and 3 still apply before live SMS. STOP/HELP replies are handled by Twilio and need no app work (#192).

1. Confirm the policy's statements about the actual data, service providers, retention, and no marketing use are true.
2. Resolve the difference between the registered Twilio A2P brand and the public program name with Twilio before relying on these pages for campaign approval.
3. Publish and check both policy URLs without a login.
4. Put the same URLs in the Twilio campaign and in the in-person disclosure.
5. STOP and HELP replies are Twilio-managed (#192); no app configuration is planned.

**Pre-pilot note (#91).** In `dev`, Advanced Opt-Out is enabled with CANCEL removed from its opt-out keywords. CANCEL is on the CTIA standard opt-out list and in the approved campaign's registered opt-out keywords, so the two disagree. Before the pilot, either update the campaign registration or restore CANCEL; the owner has not chosen between them.

Sources: [Twilio error 30896 guidance](https://www.twilio.com/docs/api/errors/30896), [Twilio registration quickstart](https://www.twilio.com/docs/messaging/compliance/a2p-10dlc/quickstart), [Twilio business information guidance](https://www.twilio.com/docs/messaging/compliance/a2p-10dlc/collect-business-info), and [Twilio consent-flow checklist](https://www.twilio.com/docs/api/errors/30924).
