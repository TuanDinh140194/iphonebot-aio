# iPhoneBot-v10.3.18-AIO


## v10.3.18 automatic Windows Chrome startup

- You no longer need to pre-open Chrome or run `start_chrome_bot.bat` before a checkout.
- When checkout starts and CDP is not already available, Python asks Windows to launch the installed `chrome.exe` directly with remote debugging on port 9222 and the dedicated `%LOCALAPPDATA%\iPhoneBot-v10-Chrome` profile.
- Playwright starts only after that real Chrome window is up and its CDP endpoint is ready; Playwright never launches Chromium in the configured CDP flow.
- The initially opened blank Chrome tab is reused and navigated to the Apple product URL after Playwright attaches.
- If Chrome is already running on the bot CDP port, the bot simply attaches to it instead of opening another window.
- `start_chrome_bot.bat` remains available only as a manual fallback.

## v10.3.16 immediate card autofill after billing ZIP

- Payment visibility checks use combined exact selectors so saved-card autofill starts as soon as Apple's card module appears.
- Billing/contact fallback waits are shorter while preserving Apple's formatter/validation behavior.
- After CVV is filled, the bot blurs the field so Apple's encryption/validity state commits before Continue.
- `#checkout-billing-continue` is scrolled into view and allowed up to 10 seconds to become enabled instead of giving up after 1 second.
- If the normal Playwright click fails, the bot may DOM-click only that exact HAR-confirmed Billing -> Review control.
- Review detection also recognizes Apple's live `#checkout-review-continue`; the bot still never clicks the final **Place Your Order** control.


## v10.3.11 faster Pickup Details + CREDIT payment selection

- Pickup Details detection now uses a fast live-form check so Profile autofill starts as soon as the contact form mounts.
- Reduced unnecessary waits around contact fields and phone formatting while preserving Apple's input formatter.
- Billing is recognized as soon as **Credit or Debit Card** is visible; the bot no longer waits for card fields that do not exist yet.
- The bot explicitly selects Apple's HAR-confirmed `CREDIT` billing option, waits for the credit-card module to mount, then fills billing address and saved card data.
- Final `Place Your Order` remains hard-blocked and is never clicked.


## v10.3.10 Pickup Details autofill

After the pickup time window is accepted and Apple opens **Pickup Details**, the bot now waits for the HAR-confirmed contact inputs themselves, fills pickup first/last name (falling back to profile first/last name), email, and phone, then continues to Payment. Transition waits ignore stale React markup from the previous checkout stage.

## v10.3.10 exact Available Time Window handling

The checkout assistant now targets Apple's live pickup-time `<select>` directly by its exact ID `checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.timeSlotValue`. It scrolls to and clicks that control before choosing the first enabled non-empty time option, then verifies the selected value before continuing. This removes the older dependency on a wrapper selector that could prevent the dropdown code from running at all.


HAR-grounded Apple pickup checkout assistant. This build keeps the stock monitor and CDP Chrome workflow, then follows the observed Apple checkout sequence through Order Review and always stops before the final purchase control.

## v10.3.10 Available Time Window interaction

The pickup time flow now explicitly opens Apple's **Available Time Window** dropdown before selecting a slot. The bot scrolls/focuses/clicks the HAR-confirmed `select[data-autom="pickup-availablewindow-dropdown"]`, waits for non-empty options, tries keyboard selection (`ArrowDown` + `Enter`), verifies the selected value, and uses Playwright `select_option()` only as a fallback after the dropdown has been opened.

## Full-auto test flow

1. Detect configured iPhone pickup stock.
2. If no bot Chrome is running, Windows automatically opens Google Chrome with the bot CDP profile; Playwright then attaches to that Chrome window.
3. Open the exact configured Apple product page.
4. Select visible product options and pickup.
5. Click Add to Bag and verify the expected model in Apple's real Bag.
6. Click Checkout and Continue as Guest when Apple presents sign-in.
7. Fulfillment: select pickup/store and the first available check-in time window.
8. Pickup Details: fill pickup name, email, and phone from the selected Profile.
9. Billing: select Credit or Debit Card and fill billing address.
10. Payment: read the Profile's card number, expiration, and CVV from Windows Credential Manager and fill Apple's real payment fields.
11. Click Apple's Billing -> Review control.
12. Stop as soon as Order Review / `Place Your Order` is visible.

The bot never intentionally clicks `Place Your Order`, `Place Order`, `Pay Now`, `Buy Now`, `Submit Order`, `Complete Purchase`, `Order Now`, or `Confirm Purchase`.

## Profile data

The Profile form includes:

- profile name
- first and last name
- email
- phone
- optional pickup first/last name override
- billing street / apt or unit
- city / state / ZIP / country
- card number
- expiration month/year
- CVV
- optional name on card

Normal contact/address fields are stored in SQLite. Card number, expiration, CVV, and name-on-card are stored using the OS credential vault (`keyring`; Windows Credential Manager on Windows). SQLite stores only the card last four digits for display. The API never returns the full card or CVV.

## HAR-grounded Apple controls

v10.3 uses the observed Apple checkout stages and exact control IDs where available:

- Fulfillment continue: `#checkout-fulfillment-continue`
- Pickup Contact continue: `#checkout-pickupContact-continue`
- Billing -> Review continue: `#checkout-billing-continue`
- Final purchase detection only: `#checkout-review-placeOrder-continue`

The final selector is detection-only and is never clicked.

## Windows setup

Run once:

```bat
setup_windows.bat
```

Then each session:

```text
1. start_bot.bat
2. Open http://127.0.0.1:8080
3. When checkout is triggered, Windows opens the dedicated Google Chrome window automatically.
4. Playwright attaches to that window and navigates it to Apple.
```

You may still run `start_chrome_bot.bat` manually if you want to open/sign in to the dedicated Chrome profile before testing, but it is no longer required.

The Chrome profile is stored at:

```text
%LOCALAPPDATA%\iPhoneBot-v10-Chrome
```

## Full Checkout task requirements

A Full Checkout task requires:

- Browser Assist enabled
- Attempt Add to Bag enabled
- Full Checkout enabled
- a selected Profile
- saved payment on that Profile

The dashboard and backend both validate that the selected Profile has saved payment metadata before creating a Full Checkout task.

## Verification and anti-bot steps

The assistant does not bypass CAPTCHA, Apple verification, identity checks, 3-D Secure, bank verification, purchase limits, access controls, or anti-bot checks. If one appears, the browser remains open and the task changes to `ACTION_REQUIRED`.

## Checkout statuses

Typical successful flow:

```text
MONITORING
-> STOCK_FOUND
-> CHECKOUT_READY
-> PREPARING_CHECKOUT
-> SELECTING_OPTIONS
-> CARTING
-> FILLING_CHECKOUT
-> SELECTING_FULFILLMENT
-> FILLING_CHECKOUT
-> FILLING_PAYMENT
-> PAYMENT_FILLED
-> READY_TO_PLACE_ORDER
```

`READY_TO_PLACE_ORDER` means Apple Order Review is visible and the final purchase button has not been clicked.

## Security notes

- Do not share `config.json` publicly if it contains a real Telegram bot token.
- Do not share HAR files publicly; HAR captures can contain cookies, session identifiers, personal information, and payment-related state.
- Card secrets are not packaged in the ZIP. They remain in the Windows credential vault for the Windows account that saved them.



## v10.3.10 pickup time-slot fix

Apple checkout renders pickup **dates** as radio buttons, but the normal check-in **time windows** are a dropdown with `data-autom="pickup-availablewindow-dropdown"`. v10.3.10 now selects the first available date when needed, then selects the first enabled non-empty time-window option from that dropdown and verifies the selected value before continuing. Recommended check-in radio slots and semantic time-range controls remain as fallbacks.

## v10.3.10 pickup fulfillment fix

After **Continue as Guest**, Apple checkout initially selects HOME delivery. v10.3.10 explicitly selects the HAR-confirmed **RETAIL / “I’ll pick it up”** fulfillment option, waits for the pickup module/store locator to render, and only then searches the ZIP/store/time slot. The pickupTab is treated as a loaded module, not a clickable mode selector.
### Payment vault status (v10.3.16)
The dashboard now checks whether the selected Profile's payment secret is actually readable from the OS credential vault. A stored last-four value in SQLite is only metadata and does not prove that the card secret exists. If the UI says `Vault missing`, open Profiles, choose `Add card`, and save the card again under the same Windows account that runs the bot.
