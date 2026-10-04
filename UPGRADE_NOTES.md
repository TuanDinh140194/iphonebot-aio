# v10.3.18

## ZIP-to-card fast path

- After Billing ZIP, try the exact HAR-observed Card Number / Expiration / Security Code fields on the main page immediately.
- Scan frames only as a short fallback when an exact field is missing.
- Reduced artificial typing delay while preserving real keyboard/input/blur events.
- No changes to Guest, Pickup, time-slot, or Billing Continue flow.


- Adds automatic Windows Chrome startup for CDP mode.
- If `http://127.0.0.1:9222` is not already available, the bot locates installed Google Chrome and launches `chrome.exe` directly via Windows/Python using the dedicated `%LOCALAPPDATA%\iPhoneBot-v10-Chrome` user-data directory.
- Chrome starts before Playwright; only after the CDP endpoint is ready does Playwright start and attach with `connect_over_cdp`.
- Reuses the blank tab opened by Windows Chrome for the Apple URL instead of requiring a pre-opened browser.
- Keeps `start_chrome_bot.bat` as a manual fallback and keeps all v10.3.16 checkout/payment behavior unchanged.

# v10.3.16

- Starts Card Number immediately after billing ZIP blur instead of waiting for City/State lookup.
- Finishes City/State after card autofill, before Billing Continue.
- Keeps Guest, Pickup, time-slot, contact, CREDIT selection, and final Place Your Order guard unchanged.

- Regression fix: restores the entire standard Checkout -> Continue as Guest routine from v10.3.12, which was confirmed working.
- Specifically restores the post-Checkout settle delay from 250 ms back to 900 ms before searching for `#signIn-guestLogin`; no other pre-Payment checkout behavior was changed.
- Keeps the v10.3.13 Payment speed and exact Billing Continue improvements.
- Starts Billing/payment autofill sooner by avoiding serial selector timeouts.
- Shortens non-essential billing address settle delays.
- Blurs CVV after fill so Apple can finish client-side payment validation.
- Waits for the exact `#checkout-billing-continue` control to become enabled, scrolls it into view, and advances to Order Review.
- Retries one Apple validation round-trip if Billing remains active.
- Recognizes `#checkout-review-continue` as the live Review-page final control for detection only; it is never clicked.

# v10.3.11

- Faster transition into Pickup Details autofill; fewer sequential visibility waits and shorter phone typing delay.
- Detects Billing from the visible **Credit or Debit Card** payment option before card fields exist.
- Selects `checkout.billing.billingOptions.selectBillingOption=CREDIT` via the live UI, waits for the card module, then fills billing/card fields.
- If CREDIT cannot be activated, stops with `ACTION_REQUIRED` instead of wasting time trying invisible card fields.
- Still stops before `Place Your Order`.

# v10.3.10

- Fix Apple Pickup Details phone autofill. Apple validates the pickup phone as `(###) ###-####` and its checkout formatter expects digit-by-digit input events. The bot now normalizes the profile phone to 10 US digits, types the digits sequentially into the HAR-confirmed phone field, blurs the field, and verifies Apple retained all 10 digits before continuing.

# iPhoneBot v10.3.10 AIO upgrade notes

## v10.3.10 Pickup Details transition/autofill fix

- Detects Pickup Details from the live First Name / Last Name / Email / Phone fields, not only the Continue button.
- After **Continue to Pickup Details**, ignores stale Fulfillment DOM and waits for the real Pickup Contact stage.
- Waits for the self-pickup contact form before filling Profile data and logs only boolean fill results (never contact values).
- After **Continue to Payment**, ignores stale Pickup Contact DOM and waits for Billing before payment autofill.

## v10.3.10 post-RETAIL time-window progression fix

- Treats `aria-checked="true"` on **I’ll pick it up** as a successful RETAIL selection instead of waiting for legacy pickup-module selectors.
- Immediately looks for the exact live `Available Time Window` select after RETAIL and scrolls/selects it before optional store-search fallback.
- The store-search input is no longer a blocking prerequisite; Apple may carry the selected store into checkout.


## v10.3.10 exact Available Time Window fix

The pickup time selector is now targeted by the exact live Apple ID `checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.timeSlotValue` (with `data-autom="pickup-availablewindow-dropdown"` as fallback). The old wrapper/container visibility gate was removed because it could return before the real `<select>` was processed. The bot now scrolls the exact select into the center of the viewport, focuses and clicks it, waits for enabled non-empty `<option>` values, selects the first available window, and verifies that the selected value persisted before continuing.


## v10.3.10 Available Time Window interaction fix

Apple's pickup check-in control is a native `<select>` labelled **Available Time Window**. The bot now explicitly scrolls to, focuses, and clicks that dropdown before choosing a slot. It then tries `ArrowDown` + `Enter` like a user and falls back to `select_option()` only after the dropdown has been opened, while verifying the resulting non-empty value.

v10.3 is based on the full Apple checkout HAR supplied for testing and focuses on reliable Bag -> Order Review automation.

## Checkout changes

- Added explicit checkout stage detection: Fulfillment, Pickup Contact, Billing, Review.
- Added checkout-level pickup/store handling after Bag.
- Added automatic selection of the first visible pickup check-in window.
- Added HAR-confirmed Pickup Contact field selectors.
- Added HAR-confirmed Billing address field selectors and ZIP/city-state handling.
- Added HAR-confirmed payment field selectors for card number, expiration, and security code.
- Billing -> Review now clicks only `#checkout-billing-continue`, because Apple's observed button label is the generic word `Continue`.
- Added one Billing validation retry, matching the observed behavior where Apple may remain on Billing after ZIP/card validation.
- Final Order Review detection recognizes both `Place Order` and `Place Your Order`.
- Added exact final selector detection for `#checkout-review-placeOrder-continue`; this control is never clicked.

## Profile changes

- The dashboard Profile form requires saved payment for full-auto profiles.
- Full Checkout task creation validates that the selected Profile has payment saved.
- Card secrets remain in the OS credential vault and are not stored in SQLite.

## Existing behavior retained

- Attach to pre-opened Chrome over CDP.
- Do not close the user's Chrome on bot shutdown.
- Verify the expected iPhone in Apple's real Bag.
- Store confirmation on the product page is non-blocking.
- Stop on CAPTCHA / verification / 3-D Secure / bank verification.
- Never place the final order automatically.

## v10.3.10 fixes

- Pauses dashboard polling while Add card / Replace card is open so payment inputs are not reset.
- Keeps React source and the served dashboard feature behavior aligned for payment management.
- Uses HAR-confirmed `#cart-actions-checkout` for standard checkout and never chooses the Apple Pay checkout branch.
- Uses HAR-confirmed `#signIn-guestLogin` for Continue as Guest, with exact-label fallbacks only.
## v10.3.16
- Payment stage now activates Apple's **Credit or Debit Card** option before checking for a local card secret.
- If the OS credential vault is missing the card secret, the browser remains open on the real credit-card form and the task reports a precise recovery message.
- Dashboard profile cards now distinguish SQLite last-four metadata from an actually readable OS-vault secret.
- Add/Replace Payment now verifies a successful round-trip read from the OS credential vault before reporting success.
- Full checkout task validation now requires an actual readable secure payment secret, not just `payment_last4` metadata.


## v10.3.16
- Preserves the previously working checkout/guest/fulfillment/pickup flow unchanged.
- Card number, expiration and CVV now use real keyboard input + Tab/blur so Apple card validity/encryption bindings commit before Continue.
- Billing Continue no longer depends on a separate is_enabled() gate; it targets the HAR-confirmed #checkout-billing-continue first and exact-text Continue only while the checkout stage is Billing.
- Final Place Your Order remains detection-only and is never clicked.
