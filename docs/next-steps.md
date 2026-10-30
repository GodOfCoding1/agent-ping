# Next steps: release AgentPing on Google Play

Updated 2026-10-30. **Current status: production-connected personal build, ready
to prepare internal testing; not yet ready for public Play Store release.**
The backend is already hosted. The next work is deletion/privacy, release
packaging, acceptance testing and Play review—not another hosting or OAuth setup.

## Completed baseline

Auth0/Google browser login, scoped REST/MCP, refresh/reconnect, revocation,
account portal and one-time pairing are implemented. Cloud Run API/private worker,
Cloud Tasks, hourly recovery and Neon schema `0004` are deployed.

Signed/minified **AgentPing 0.2.0**, versionCode 2, `com.errorfactory.agentping`,
is installed as the Samsung's primary production device. FID registration, sync,
notifications and exact alarm permission are verified. A real production MCP push
returned provider acceptance, device receipt and display evidence; its physical
sound was not independently observed. Credentials/replicas/actions are scoped by
backend URL. Local release signing and future signed installs are supported.

Last recorded results: 180 backend tests at the prior OAuth milestone, then 7
Android JVM tests and 12 emulator tests at cutover, with passing debug/release
builds/lint. See [verification](verification.md) and [agent handoff](implementation-handoff.md)
for exact scope. Earlier local/emulator alarm evidence is not production phone acceptance.

Use **AgentPing**, not **AgentPing Dev**, for production: the worker restricts
pushes to the release package, so `.debug` cannot receive them. No AAB, Play
Console eligibility, Play signing enrollment or store upload has been verified.

## 1. Implement account deletion and complete privacy behavior

- [ ] Implement account **and associated-data deletion**. Current `close_account`
  cancels reminders/revokes access but retains account/activity/application records.
  Define what is erased, completion timing, Auth0 identity handling, justified
  retention and backup expiry. A deleted-status flag is not data erasure.
- [ ] Add obvious Settings links to privacy/account management/deletion, plus an
  external deletion-request URL usable without the installed app. A browser flow
  is acceptable if it actually supports the documented request/deletion behavior.
- [ ] Test a disposable account: revoke credentials/sessions/grants, stop pending
  delivery, delete/anonymize intended data, prevent background resurrection and
  confirm another account is unaffected. Do not erase the owner's account as a test.
- [ ] Expand existing `/privacy` with actual collection/sharing, SDK behavior,
  retention/deletion and support/developer contact details; link it in the app.

Directing users to browser signup still falls within
[Play's account deletion requirements](https://support.google.com/googleplay/android-developer/answer/13327111?hl=en).
Any production migration/deletion needs an authorized rollout; this checklist is
not permission to delete real customer data.

**Done when:** tested deletion, external request URL, in-app entry points and
privacy disclosures all agree with the implemented behavior.

## 2. Confirm Play account eligibility and signing continuity

This administrative work can begin alongside deletion implementation.

- [ ] Confirm the owning Play Console account exists/is verified, its personal or
  organization type, creation date, applicable testing gates and ability to create
  `com.errorfactory.agentping`. None of that account state has been inspected.
- [ ] Privately back up `.agentping/android-signing/agentping-release.jks` and
  ignored `android/signing.properties`. Decide how the installed signing
  certificate maps to Play App Signing versus its upload key. Preserve compatible
  updates, or explicitly plan a reinstall/re-pair transition before choosing a
  different app signing key. Matching the upload key alone is insufficient.
- [ ] Verify the production Firebase package/configuration and any applicable
  provider certificate restrictions against the actual Play app signing certificate.

For personal accounts created after November 13, 2023, plan a closed test with
**at least 12 testers opted in continuously for at least 14 days**, then an
application for production access. Applicability is unverified for this user's
account. Local/emulator tests and internal testing do not replace that gate.
[Official testing requirements](https://support.google.com/googleplay/android-developer/answer/14151465?hl=en)

**Done when:** actual eligibility/testing requirements, a protected key backup and
an explicit signing/update strategy are recorded.

## 3. Produce and validate the Play artifact

- [ ] Build the signed release **Android App Bundle** using
  `./dev android-gradle :app:bundleRelease`. This is a future action, not a completed
  check. Existing `android-release` builds an APK and runs JVM/lint checks, not an AAB.
- [ ] Inspect/validate the bundle and generated APKs: release application ID,
  production HTTPS, Firebase enabled, non-debuggable build, intended permissions,
  no embedded backend secrets and supported-device behavior. Retain R8 mappings
  and native symbols where applicable.
- [ ] Review relevant lint findings and native library/16 KB compatibility.
  Current lint has 0 errors/29 advisories; this is not Play acceptance. Inspect
  transitive native libraries rather than assuming Kotlin means no native code.
- [ ] Keep version codes increasing for subsequent uploads. Current target/compile
  SDK 36 meets the published phone-app target requirement checked on 2026-10-30;
  recheck current Console requirements when uploading.

New Play apps use AABs and Play App Signing; see
[release preparation](https://support.google.com/googleplay/android-developer/answer/9859348?hl=en),
[target API requirements](https://support.google.com/googleplay/android-developer/answer/11926878?hl=en)
and [16 KB compatibility](https://developer.android.com/guide/practices/page-sizes).

**Done when:** a validated AAB, reproducible command, signing/version details and
compatibility results exist. Producing the bundle does not publish the app.

## 4. Close production acceptance gaps

- [ ] A second independent Google account completes portal login, pairing and MCP
  consent/refresh. Prove cross-account reminder/device/activity/grant access is
  denied and real delivery reaches the correct account's phone. Existing automated
  isolation tests do not close this live acceptance gate.
- [ ] Verify a future scheduled alarm on the signed production app: downloaded
  schedule, due time, notification, **heard sound**, snooze/ack/dismiss. Record
  provider acceptance, device receipt, precise local scheduling and display separately.
- [ ] Verify downloaded alarms without networking, offline action replay, reboot
  then unlock without opening the app, background/Doze, cancellation and permission
  denial/revocation/recovery. Use an agreed test device for disruptive exercises.
- [ ] Exercise release UI create/edit/pause/resume/cancel, recurrence, failures,
  credential expiry/unpair/re-pair, large fonts/TalkBack and representative Android/
  OEM behavior. Document supported-device limitations and fix core-feature defects.
- [ ] After internal upload, repeat onboarding/pairing/permissions/delivery using
  an actual **Play-installed** build. Generated APKs, signing and full-screen
  permission behavior need validation independently of sideloading.

Use the [manual matrix](android-manual-tests.md), labelling new evidence as
production/release/Play. Do not silently interrupt the owner's phone or count
historical local/emulator results as completed production acceptance.

**Done when:** dated evidence covers advertised core phone features and the
second live user; unresolved limitations are reflected in the product/listing.

## 5. Prepare listing, policy declarations and reviewer access

- [ ] Prepare final name/descriptions/icon, screenshots/feature graphic, support
  contact, countries, audience, content rating, ads/pricing declarations and accurate
  Data Safety answers based on an actual data/SDK inventory.
- [ ] Provide public privacy and deletion-request URLs. Align the app, website and
  listing with actual retention/deletion behavior.
- [ ] Review the actual manifest: `SCHEDULE_EXACT_ALARM`, `USE_FULL_SCREEN_INTENT`
  and ringing service types `mediaPlayback|systemExempted`. Justify eligible/used
  types, remove inappropriate declarations, and prepare Console explanations and
  demonstration video where required. Sideloaded full-screen access is not approval
  for Play eligibility or unrestricted alerts.
- [ ] Give reviewers a reproducible route through login, pairing and core agent/
  reminder features. Do not depend on the owner's Google account, local tooling
  or an expired pairing code. Keep required review credentials private in Console,
  never in repo docs; do not create an authentication bypass.

See [review preparation](https://support.google.com/googleplay/android-developer/answer/9859455?hl=en)
and [foreground-service/full-screen requirements](https://support.google.com/googleplay/android-developer/answer/13392821?hl=en).

**Done when:** listing/declarations are accurate, required policy evidence exists
and reviewers can exercise the app without developer intervention.

## 6. Establish operations for the initial audience

- [ ] Verify the configured Auth0 flows/quotas work after signup-trial expiry on
  the intended Free plan. No paid upgrade is authorized by this roadmap.
- [ ] Establish regular encrypted off-machine database backups and a restore drill.
  Prior one-off restores passed; an ongoing backup routine is not verified.
  Back up/recover signing material separately and privately.
- [ ] Establish actionable crash/ANR, delivery/queue failure and service-health
  monitoring, support ownership and incident/rollback procedures. Assess existing
  logging/Play vitals before adding another service or data-collecting SDK.
- [ ] Measure beta usage/billing and verify the existing durable account/client
  quotas and abuse limits for the planned audience. Size initial access within
  INR 500/month. The INR 0–300 light-use estimate is not a hard cap or public-scale
  capacity guarantee. Keep Neon Free and request-driven scale-to-zero; obtain
  approval for paid changes. No continuous worker, Railway or automation-server changes.

**Done when:** recovery/monitoring ownership, tested rollback and measured cost/
capacity assumptions support the proposed initial audience.

## 7. Test through Play, then request public release

- [ ] Upload the validated AAB to **internal testing** first. Review pre-launch
  reports and actual Play installs, fix crashes/compatibility/policy issues and
  complete the Play-installed acceptance cases above. Internal testing can start
  before every public-launch gate closes.
- [ ] Run a closed beta and retain real feedback/fixes. If the personal-account
  12-tester/14-day gate applies, meet it and apply for production access; approval
  is not automatic after the timer finishes.
- [ ] Close gates 1–6 and all applicable Console requirements before public
  submission; record release evidence, known limits and a bounded initial rollout.
- [ ] Obtain the user's explicit public-publication approval and approval for any
  new spend, submit for Google review, address findings and monitor the approved
  rollout. An uploaded bundle is not an approved/live listing.

Custom DNS, native Android OAuth and a separate Wear OS app are not prerequisites
for this phone app's working browser-pairing design. Watch work remains deferred;
omit unsupported watch promises. Do not promise guaranteed delivery, DND bypass,
exactly-once ringing or before-first-unlock alarms. This is the launch roadmap,
not a claim of Play approval or authority to publish now.
