# iPhoneBot v10.3.2 AIO frontend

The served dashboard is in `frontend/dist` and the React/Vite source is in `frontend/src`.

v10.3 Profile setup collects contact, pickup, billing, and secure payment information. Card secrets are sent only to the local FastAPI backend and stored through the OS credential vault; they are not returned by the API or stored in SQLite.

Full Checkout requires a Profile with saved payment and follows the HAR-mapped Apple checkout flow through Order Review, then stops before `Place Your Order`.
