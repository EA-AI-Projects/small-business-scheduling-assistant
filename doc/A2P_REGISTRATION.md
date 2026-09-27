# A2P registration notes for the scheduling pilot

The public policy pages are in `pages/`. They describe the Small Business Scheduling Assistant proof of concept and contain no placeholder brand or contact details. The stated support email is `ealeman.kikito@gmail.com`. Review the policies against actual behavior before publishing or submitting them to Twilio.

## Sender and consent

The registered Twilio A2P brand is an individual's name, while these public pages identify only the proof-of-concept program. Twilio may reject a campaign if the sender identified in the policies, consent request, HELP reply, and sample messages cannot be connected clearly to the registered brand. Do not assert that a fictional cleaning business is a real sender. Before production use by a real business, register the appropriate business sender and align all consumer-facing disclosures with it.

The planned opt-in is verbal before a participant is enrolled in the app. The person enrolling the participant should explain the scheduling message program, ask for a clear yes/no response, and retain a record of consent (participant, date/time, method, and the version of the disclosure used). A refusal must not prevent someone from obtaining the underlying service. Do not send an initial automated enrollment text to obtain consent from someone who has not opted in.

Suggested verbal disclosure, to be used only once the brand and links are live:

> Would you like to receive text messages from the **Small Business Scheduling Assistant** proof of concept about test appointment scheduling, including scheduling requests, confirmations, changes, and cancellations? Message frequency varies with your scheduling activity and replies. Message and data rates may apply. Reply **HELP** for help or **STOP** to opt out at any time. Our terms are at **[TERMS URL]** and our privacy policy is at **[PRIVACY URL]**. Text consent is optional and is not required to receive any underlying service. Please say yes or no.

The Twilio `message_flow` should describe exactly where this conversation occurs (for example, in person or by phone), who delivers this disclosure, how a clear yes is recorded, and what confirmation follows. Include the full script and both public links. If any other opt-in method is used, describe it too. Verbal consent is not sufficient for marketing texts; this program is drafted for scheduling messages only.

## Before submitting

1. Confirm the policy's statements about the actual data, service providers, retention, and no marketing use are true.
2. Resolve the difference between the registered Twilio A2P brand and the public program name with Twilio before relying on these pages for campaign approval.
3. Publish and check both policy URLs without a login.
4. Put the same URLs in the Twilio campaign and in the verbal disclosure.
5. Configure and test STOP and HELP replies and ensure sample messages identify the registered sender.

Sources: [Twilio A2P onboarding guide](https://help.twilio.com/articles/11847054539547), [Twilio business information guidance](https://www.twilio.com/docs/messaging/compliance/a2p-10dlc/collect-business-info), and [Twilio consent-flow checklist](https://www.twilio.com/docs/api/errors/30924).
