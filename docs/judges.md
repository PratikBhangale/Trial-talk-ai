# Trying TrialTalk (for judges)

TrialTalk lets clinical-trial participants talk to an AI study companion on WhatsApp, and lets study staff manage dosing reminders, requests and emergencies in a web portal. Everything here is synthetic demo data.

You will play the study coordinator, enroll yourself as a participant, and watch both sides. It takes about 10 minutes. You need a phone with WhatsApp.

- **Staff portal:** https://main.dqzri9vvxpyky.amplifyapp.com/staff/
- **Participant website:** https://main.dqzri9vvxpyky.amplifyapp.com/participant/
- **TrialTalk WhatsApp number:** +1 609 642 3667, or open https://wa.me/16096423667 on your phone
- **Staff login:** the email and password the team gave you (a shared demo login, so you may see other judges' demo participants)

## 1. Say hello on WhatsApp (1 min)

Message the TrialTalk WhatsApp number with "hi". This also opens WhatsApp's 24-hour window, which the reminders below need. Ask a study question, such as "can I drink alcohol during the study?". The agent answers from the approved participant guide.

## 2. Enroll yourself as a participant (2 min)

1. Open the staff portal and sign in with the staff login above.
2. Go to **Participants** and choose **Enroll participant**. Enter any name, your WhatsApp number in international format (for example `+14155550123`), Riverside as the site, study KOA-301, and turn **WhatsApp reminders** on.
3. After saving, the portal shows a **temporary password** once. Copy it.

The form's note says to use 555 numbers; ignore that for this demo, because a 555 number cannot receive WhatsApp messages. Your number is stored only in this demo database, and the team removes it afterwards.

## 3. Get a dose reminder on your phone (2 min)

1. Open your new participant and create a **dose reminder** (for example label "morning", instructions "1 tablet with food", a time a few minutes from now).
2. Press **Send now**. The reminder arrives on WhatsApp within seconds.
3. Tap **Taken** or **Skipped**. In the portal, open **Records**: **Adherence** and the **Clinical timeline** show your answer, and **Communications** shows each message and its delivery status.

If nothing arrives, send "hi" to the number again (step 1) and press Send now again.

## 4. Raise an emergency (2 min)

On WhatsApp, send `chest pain`. You are told to call emergency services. In the staff portal, open **Emergency alerts**: the alert appears, and you can **Acknowledge** and then **Resolve** it. **Requests** also lists it under "Needs attention".

## 5. Use the participant website (2 min)

Open the participant website in a **private or incognito window**, or a second browser. The staff portal and the participant website share one sign-in per browser, so signing in to one signs you out of the other. Sign in with your phone number (exactly as you entered it, for example `+14155550123`) and the temporary password, then choose a new password (at least 8 characters). See your dose schedule and history, send a request to the study team, then reply to it from **Requests** in the staff portal and reload the participant page to see the reply.

## If something does not work
- **No WhatsApp reminder:** send "hi" to the number again, then press **Send now** again. WhatsApp only delivers if you messaged the number in the last 24 hours.
- **No reply to "chest pain" or no alert:** the alert needs an enrolled participant with your number and **WhatsApp reminders** turned on (step 2).
- **Cannot sign in to the participant website:** use the temporary password shown at enrollment. In the staff portal, open your participant and choose **Reset login** to get a new one.
- **Page looks empty:** reload it.

## Also worth a look
**Overview** (the daily briefing), **Observability** (traces and latency), **Token usage**, and **Team**.

## Please
- Use only synthetic information. Do not type real medical details.
- Cancel your reminder when you finish, so it does not message you again.
