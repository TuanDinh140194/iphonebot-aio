import { useCallback, useEffect, useMemo, useState } from "react";
import Logo from "./components/logo";

const API_URL = "";

const STATUS_LABELS = {
  MONITORING: "Monitoring",
  STOCK_FOUND: "Stock Found",
  WAITING_FOR_MONITOR_PING: "Waiting For Monitor Ping",
  PREPARING_CHECKOUT: "Preparing Checkout",
  SELECTING_OPTIONS: "Selecting Options",
  SELECTING_FULFILLMENT: "Selecting Fulfillment",
  CARTING: "Carting",
  FILLING_CHECKOUT: "Filling Checkout",
  FILLING_PAYMENT: "Filling Payment",
  PAYMENT_FILLED: "Payment Filled",
  READY_TO_PLACE_ORDER: "Ready to Place Order",
  CHECKOUT_READY: "Checkout Ready",
  ACTION_REQUIRED: "Action Required",
  RATE_LIMITED: "Rate Limited",
  ERROR: "Error",
  STOPPED: "Stopped",
  CARTED: "Carted",
  SUCCESS: "Success",
};

function statusClass(status) {
  if (["STOCK_FOUND", "SUCCESS", "CARTED", "CHECKOUT_READY", "PAYMENT_FILLED", "READY_TO_PLACE_ORDER"].includes(status)) return "good";
  if (["ACTION_REQUIRED", "PREPARING_CHECKOUT", "SELECTING_OPTIONS", "SELECTING_FULFILLMENT", "CARTING", "FILLING_CHECKOUT", "FILLING_PAYMENT"].includes(status)) return "hot";
  if (["RATE_LIMITED", "ERROR"].includes(status)) return "bad";
  if (["STOPPED"].includes(status)) return "muted";
  return "info";
}

async function api(path, options = {}) {
  const response = await fetch(`${API_URL}${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(data?.detail || `Request failed (${response.status})`);
  }
  return data;
}

function Metric({ label, value, accent = false }) {
  return (
    <div className={`metric-card ${accent ? "accent" : ""}`}>
      <span>{label}</span>
      <strong>{value ?? 0}</strong>
    </div>
  );
}

function App() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [showProfiles, setShowProfiles] = useState(false);
  const [paymentProfileId, setPaymentProfileId] = useState(null);

  const [form, setForm] = useState({
    model_code: "",
    name: "",
    product_url: "https://www.apple.com/shop/buy-iphone/iphone-18-pro",
    zipcode: "97220",
    stores: "R077, R090, R134",
    poll_min: "55",
    poll_max: "65",
    proxy_url: "",
    profile_id: "",
    auto_prepare_browser: true,
    auto_add_to_bag: true,
    auto_checkout: true,
  });

  const emptyProfile = {
    name: "",
    first_name: "",
    last_name: "",
    email: "",
    phone: "",
    pickup_first_name: "",
    pickup_last_name: "",
    address1: "",
    address2: "",
    city: "",
    state: "OR",
    postal_code: "",
    country: "US",
    card_number: "",
    exp_month: "",
    exp_year: "",
    cvv: "",
    name_on_card: "",
  };
  const [profileForm, setProfileForm] = useState(emptyProfile);
  const emptyPayment = {
    card_number: "",
    exp_month: "",
    exp_year: "",
    cvv: "",
    name_on_card: "",
  };
  const [paymentForm, setPaymentForm] = useState(emptyPayment);

  const refresh = useCallback(async (quiet = false) => {
    try {
      const next = await api("/api/dashboard");
      setData(next);
      setError("");
    } catch (err) {
      if (!quiet) setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    // Do not re-render profile/payment forms while sensitive fields are being edited.
    if (showProfiles || paymentProfileId) return undefined;
    const timer = setInterval(() => refresh(true), 3500);
    return () => clearInterval(timer);
  }, [refresh, showProfiles, paymentProfileId]);

  const monitors = useMemo(
    () => (data?.tasks || []).filter((task) => task.mode === "monitor"),
    [data],
  );

  const checkouts = useMemo(
    () => (data?.tasks || []).filter((task) => task.mode === "instore_checkout"),
    [data],
  );

  function setField(name, value) {
    setForm((previous) => ({ ...previous, [name]: value }));
  }

  async function runAction(key, path, options = { method: "POST" }) {
    setBusy(key);
    setError("");
    try {
      await api(path, options);
      await refresh(true);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  async function createPair(event) {
    event.preventDefault();
    if (!form.model_code) {
      setError("Choose an iPhone model first.");
      return;
    }
    if (form.auto_checkout && !form.profile_id) {
      setError("Choose a checkout Profile with saved payment before enabling full checkout.");
      return;
    }
    if (form.auto_checkout && form.profile_id) {
      const selectedProfile = data.profiles.find((profile) => String(profile.id) === String(form.profile_id));
      if (!selectedProfile?.payment_last4) {
        setError("The selected Profile does not have a saved card. Add a complete Profile with payment first.");
        return;
      }
    }
    const catalogItem = data.catalog.find((item) => item.code === form.model_code);
    const payload = {
      name: form.name.trim() || catalogItem?.label || form.model_code,
      model_code: form.model_code,
      product_url: form.product_url.trim(),
      zipcode: form.zipcode.trim(),
      stores: form.stores
        .split(",")
        .map((store) => store.trim())
        .filter(Boolean),
      poll_min: Number(form.poll_min),
      poll_max: Number(form.poll_max),
      proxy_url: form.proxy_url.trim() || null,
      profile_id: form.profile_id ? Number(form.profile_id) : null,
      auto_prepare_browser: form.auto_prepare_browser,
      auto_add_to_bag: form.auto_add_to_bag,
      auto_checkout: form.auto_checkout,
      enabled: true,
    };

    setBusy("create-pair");
    try {
      await api("/api/task-pairs", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      setShowCreate(false);
      setForm((previous) => ({ ...previous, name: "", model_code: "" }));
      await refresh(true);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  async function saveProfilePayment(event) {
    event.preventDefault();
    if (!paymentProfileId) return;
    setBusy(`payment-${paymentProfileId}`);
    setError("");
    try {
      await api(`/api/profiles/${paymentProfileId}/payment`, {
        method: "PUT",
        body: JSON.stringify(paymentForm),
      });
      setPaymentProfileId(null);
      setPaymentForm(emptyPayment);
      await refresh(true);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  async function createProfile(event) {
    event.preventDefault();
    setBusy("create-profile");
    setError("");
    try {
      const profilePayload = {
        name: profileForm.name,
        first_name: profileForm.first_name,
        last_name: profileForm.last_name,
        email: profileForm.email,
        phone: profileForm.phone,
        pickup_first_name: profileForm.pickup_first_name,
        pickup_last_name: profileForm.pickup_last_name,
        address1: profileForm.address1,
        address2: profileForm.address2,
        city: profileForm.city,
        state: profileForm.state,
        postal_code: profileForm.postal_code,
        country: profileForm.country,
      };
      const created = await api("/api/profiles", {
        method: "POST",
        body: JSON.stringify(profilePayload),
      });

      await api(`/api/profiles/${created.profile.id}/payment`, {
        method: "PUT",
        body: JSON.stringify({
          card_number: profileForm.card_number,
          exp_month: profileForm.exp_month,
          exp_year: profileForm.exp_year,
          cvv: profileForm.cvv,
          name_on_card: profileForm.name_on_card,
        }),
      });

      setProfileForm(emptyProfile);
      await refresh(true);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  if (loading) {
    return <div className="loading-screen">Starting iPhoneBot v10.3.18 AIO…</div>;
  }

  const overview = data?.overview || {};
  const engine = data?.engine || {};

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <Logo />
          <div>
            <h1>iPhoneBot v10.3.18 AIO</h1>
            <p>Monitor → cart → checkout/contact/payment autofill → stop before Place Order</p>
          </div>
        </div>

        <div className="top-actions">
          <div className={`engine-pill ${engine.running ? "online" : "offline"}`}>
            <span className="dot" />
            {engine.running ? "Engine Online" : "Engine Paused"}
          </div>
          <button
            className="secondary-button"
            disabled={busy === "engine"}
            onClick={() =>
              runAction(
                "engine",
                engine.running ? "/api/engine/pause" : "/api/engine/resume",
              )
            }
          >
            {engine.running ? "Pause Engine" : "Resume Engine"}
          </button>
        </div>
      </header>

      <main>
        {error && <div className="error-banner">{error}</div>}

        <section className="metric-grid">
          <Metric label="Total Tasks" value={overview.total_tasks} />
          <Metric label="Running" value={overview.running_tasks} />
          <Metric label="Stock Hits" value={overview.stock_hits} accent />
          <Metric label="Checkout Ready" value={overview.checkout_ready} accent />
          <Metric label="Ready to Place" value={overview.ready_to_place_order} accent />
          <Metric label="Carted" value={overview.carted} />
          <Metric label="Checkouts" value={overview.checkouts} />
        </section>

        <section className="panel task-panel">
          <div className="panel-header">
            <div>
              <h2>Tasks</h2>
              <p>v10.3 follows the HAR-mapped Apple flow: product → Add to Bag → checkout → pickup/store/time slot → pickup contact → billing → saved-card payment → Order Review, then stops before Place Your Order.</p>
            </div>
            <div className="toolbar">
              <button
                className="ghost-button"
                onClick={() => runAction("start-all", "/api/tasks/start-all")}
              >
                Start All
              </button>
              <button
                className="ghost-button"
                onClick={() => runAction("stop-all", "/api/tasks/stop-all")}
              >
                Stop All
              </button>
              <button className="primary-button" onClick={() => setShowCreate((value) => !value)}>
                + Create Pair
              </button>
            </div>
          </div>

          {showCreate && (
            <form className="create-form" onSubmit={createPair}>
              <div className="form-grid">
                <label>
                  iPhone model
                  <select
                    value={form.model_code}
                    onChange={(event) => {
                      const code = event.target.value;
                      const item = data.catalog.find((entry) => entry.code === code);
                      setForm((previous) => ({
                        ...previous,
                        model_code: code,
                        name: previous.name || item?.label || "",
                      }));
                    }}
                    required
                  >
                    <option value="">Select model…</option>
                    {data.catalog.map((item) => (
                      <option key={item.code} value={item.code}>
                        {item.label} — {item.code}
                      </option>
                    ))}
                  </select>
                </label>

                <label>
                  Task name
                  <input value={form.name} onChange={(e) => setField("name", e.target.value)} placeholder="Pro Max 256 Black" />
                </label>

                <label className="wide">
                  Apple product URL
                  <input value={form.product_url} onChange={(e) => setField("product_url", e.target.value)} />
                </label>

                <label>
                  ZIP code
                  <input value={form.zipcode} onChange={(e) => setField("zipcode", e.target.value)} required />
                </label>

                <label>
                  Store IDs
                  <input value={form.stores} onChange={(e) => setField("stores", e.target.value)} placeholder="R077, R090" />
                </label>

                <label>
                  Poll min (sec)
                  <input type="number" min="5" max="3600" value={form.poll_min} onChange={(e) => setField("poll_min", e.target.value)} />
                </label>

                <label>
                  Poll max (sec)
                  <input type="number" min="5" max="3600" value={form.poll_max} onChange={(e) => setField("poll_max", e.target.value)} />
                </label>

                <label>
                  Profile
                  <select value={form.profile_id} onChange={(e) => setField("profile_id", e.target.value)}>
                    <option value="">No profile</option>
                    {data.profiles.map((profile) => (
                      <option key={profile.id} value={profile.id}>{profile.name}</option>
                    ))}
                  </select>
                </label>

                <label>
                  Monitor proxy (optional)
                  <input type="password" value={form.proxy_url} onChange={(e) => setField("proxy_url", e.target.value)} placeholder="http://user:pass@host:port" />
                </label>

                <label className="check-field">
                  Browser Assist on stock
                  <input type="checkbox" checked={form.auto_prepare_browser} onChange={(e) => setForm((previous) => ({ ...previous, auto_prepare_browser: e.target.checked, auto_add_to_bag: e.target.checked ? previous.auto_add_to_bag : false, auto_checkout: e.target.checked ? previous.auto_checkout : false }))} />
                </label>

                <label className="check-field">
                  Attempt Add to Bag
                  <input type="checkbox" disabled={!form.auto_prepare_browser} checked={form.auto_add_to_bag} onChange={(e) => setForm((previous) => ({ ...previous, auto_add_to_bag: e.target.checked, auto_checkout: e.target.checked ? previous.auto_checkout : false }))} />
                </label>

                <label className="check-field">
                  Full Checkout to Order Review
                  <input type="checkbox" disabled={!form.auto_prepare_browser || !form.auto_add_to_bag} checked={form.auto_checkout} onChange={(e) => setField("auto_checkout", e.target.checked)} />
                </label>
              </div>

              <div className="form-footer">
                <p>
                  Creates 2 linked tasks: <strong>Monitor</strong> + <strong>Instore Checkout watcher</strong>.
                  v10.3 runs the configured task automatically through pickup fulfillment, saved-card payment and Order Review, and always stops before the final Place Your Order button.
                </p>
                <button className="primary-button" disabled={busy === "create-pair"}>
                  {busy === "create-pair" ? "Creating…" : "Create Task Pair"}
                </button>
              </div>
            </form>
          )}

          <div className="task-table-wrap">
            <table className="task-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Mode</th>
                  <th>Profile</th>
                  <th>Store / Watch</th>
                  <th>Status</th>
                  <th>Last Check</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {(data.tasks || []).map((task) => (
                  <tr key={task.id} className={task.status === "ACTION_REQUIRED" ? "attention-row" : ""}>
                    <td>
                      <strong>{task.name}</strong>
                      <small>{task.model_code}</small>
                    </td>
                    <td>
                      <span className="mode-badge">
                        {task.mode === "monitor" ? "Monitor" : "Instore"}
                      </span>
                      {task.mode === "instore_checkout" && (
                        <small>
                          Assist: {task.auto_prepare_browser ? "Browser" : "Off"}
                          {task.auto_add_to_bag ? " + Bag" : ""}
                          {task.auto_checkout ? " + Checkout" : ""}
                        </small>
                      )}
                    </td>
                    <td>{task.profile_name || "—"}</td>
                    <td>
                      {task.mode === "monitor"
                        ? (task.stores?.join(", ") || "All nearby")
                        : (task.prepared_store_name
                          ? `${task.prepared_store_name}${task.prepared_store_id ? ` (${task.prepared_store_id})` : ""}`
                          : `Monitor #${task.watch_task_id ?? "—"}`)}
                    </td>
                    <td>
                      <span className={`status-badge ${statusClass(task.status)}`}>
                        <span className="dot" />
                        {STATUS_LABELS[task.status] || task.status}
                      </span>
                      {task.status_message && <small className="status-message">{task.status_message}</small>}
                    </td>
                    <td>
                      {task.last_checked_at
                        ? new Date(task.last_checked_at).toLocaleTimeString()
                        : "—"}
                    </td>
                    <td>
                      <div className="row-actions">
                        <button
                          className="icon-button"
                          title={task.enabled ? "Stop" : "Start"}
                          onClick={() =>
                            runAction(
                              `toggle-${task.id}`,
                              `/api/tasks/${task.id}/${task.enabled ? "stop" : "start"}`,
                            )
                          }
                        >
                          {task.enabled ? "■" : "▶"}
                        </button>

                        {task.mode === "instore_checkout" && (task.prepared_url || task.product_url) && (
                          <a className="open-button" href={task.prepared_url || task.product_url} target="_blank" rel="noreferrer">
                            Open Prepared
                          </a>
                        )}

                        {task.mode === "instore_checkout" && !task.prepared_url && (
                          <button className="mini-button" onClick={() => runAction(`prepare-${task.id}`, `/api/tasks/${task.id}/prepare-link`)}>
                            Prepare Link
                          </button>
                        )}

                        {task.mode === "instore_checkout" && ["CHECKOUT_READY", "ACTION_REQUIRED", "CARTED", "PAYMENT_FILLED", "READY_TO_PLACE_ORDER"].includes(task.status) && (
                          <button className="mini-button" onClick={() => runAction(`assistant-${task.id}`, `/api/tasks/${task.id}/launch-assistant`)}>
                            Browser Assist
                          </button>
                        )}

                        {task.mode === "instore_checkout" && (!task.auto_prepare_browser || !task.auto_add_to_bag || !task.auto_checkout) && (
                          <button
                            className="mini-button"
                            title="Enable Browser Assist + Add to Bag + checkout for this existing task"
                            onClick={() => runAction(
                              `full-assist-${task.id}`,
                              `/api/tasks/${task.id}`,
                              { method: "PATCH", body: JSON.stringify({ auto_prepare_browser: true, auto_add_to_bag: true, auto_checkout: true }) },
                            )}
                          >
                            Enable Full Assist
                          </button>
                        )}

                        {task.mode === "instore_checkout" && ["CHECKOUT_READY", "ACTION_REQUIRED"].includes(task.status) && (
                          <button
                            className="mini-button"
                            onClick={() => runAction(`cart-${task.id}`, `/api/tasks/${task.id}/mark-carted`)}
                          >
                            Mark Carted
                          </button>
                        )}

                        {task.mode === "instore_checkout" && ["CHECKOUT_READY", "ACTION_REQUIRED", "CARTED", "PAYMENT_FILLED", "READY_TO_PLACE_ORDER"].includes(task.status) && (
                          <button
                            className="mini-button success"
                            onClick={() => runAction(`checkout-${task.id}`, `/api/tasks/${task.id}/mark-checkout`)}
                          >
                            Mark Success
                          </button>
                        )}

                        {task.mode === "instore_checkout" && ["CHECKOUT_READY", "PREPARING_CHECKOUT", "SELECTING_OPTIONS", "CARTING", "FILLING_CHECKOUT", "FILLING_PAYMENT", "PAYMENT_FILLED", "READY_TO_PLACE_ORDER", "ACTION_REQUIRED", "CARTED", "SUCCESS"].includes(task.status) && (
                          <button
                            className="icon-button"
                            title="Reset watcher"
                            onClick={() => runAction(`reset-${task.id}`, `/api/tasks/${task.id}/reset-checkout`)}
                          >
                            ↻
                          </button>
                        )}

                        <button
                          className="icon-button danger"
                          title="Delete"
                          onClick={() => {
                            if (window.confirm(`Delete ${task.name}?`)) {
                              runAction(`delete-${task.id}`, `/api/tasks/${task.id}`, { method: "DELETE" });
                            }
                          }}
                        >
                          ×
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
                {!data.tasks?.length && (
                  <tr><td colSpan="7" className="empty-cell">No tasks yet.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </section>

        <div className="two-column">
          <section className="panel">
            <div className="panel-header compact">
              <div>
                <h2>Profiles</h2>
                <p>Contact/billing profiles. Card secrets are stored in the OS credential vault, not SQLite/config.json.</p>
              </div>
              <button className="ghost-button" onClick={() => setShowProfiles((value) => !value)}>
                {showProfiles ? "Close" : "+ Profile"}
              </button>
            </div>

            {showProfiles && (
              <form className="profile-form" onSubmit={createProfile}>
                <input placeholder="Profile name" required value={profileForm.name} onChange={(e) => setProfileForm({ ...profileForm, name: e.target.value })} />
                <div className="split-inputs">
                  <input placeholder="First name" required value={profileForm.first_name} onChange={(e) => setProfileForm({ ...profileForm, first_name: e.target.value })} />
                  <input placeholder="Last name" required value={profileForm.last_name} onChange={(e) => setProfileForm({ ...profileForm, last_name: e.target.value })} />
                </div>
                <input placeholder="Email" type="email" required value={profileForm.email} onChange={(e) => setProfileForm({ ...profileForm, email: e.target.value })} />
                <input placeholder="Phone" required value={profileForm.phone} onChange={(e) => setProfileForm({ ...profileForm, phone: e.target.value })} />
                <div className="split-inputs">
                  <input placeholder="Pickup first name (optional)" value={profileForm.pickup_first_name} onChange={(e) => setProfileForm({ ...profileForm, pickup_first_name: e.target.value })} />
                  <input placeholder="Pickup last name (optional)" value={profileForm.pickup_last_name} onChange={(e) => setProfileForm({ ...profileForm, pickup_last_name: e.target.value })} />
                </div>
                <input placeholder="Billing address" required value={profileForm.address1} onChange={(e) => setProfileForm({ ...profileForm, address1: e.target.value })} />
                <input placeholder="Apt / unit (optional)" value={profileForm.address2} onChange={(e) => setProfileForm({ ...profileForm, address2: e.target.value })} />
                <div className="split-inputs">
                  <input placeholder="City" required value={profileForm.city} onChange={(e) => setProfileForm({ ...profileForm, city: e.target.value })} />
                  <input placeholder="State" required value={profileForm.state} onChange={(e) => setProfileForm({ ...profileForm, state: e.target.value })} />
                </div>
                <div className="split-inputs">
                  <input placeholder="ZIP" required value={profileForm.postal_code} onChange={(e) => setProfileForm({ ...profileForm, postal_code: e.target.value })} />
                  <input placeholder="Country" value={profileForm.country} onChange={(e) => setProfileForm({ ...profileForm, country: e.target.value })} />
                </div>
                <div className="payment-divider">Secure payment (required for full auto)</div>
                <input autoComplete="cc-number" placeholder="Card number" inputMode="numeric" type="password" required value={profileForm.card_number} onChange={(e) => setProfileForm({ ...profileForm, card_number: e.target.value })} />
                <div className="split-inputs three">
                  <input autoComplete="cc-exp-month" placeholder="MM" inputMode="numeric" required value={profileForm.exp_month} onChange={(e) => setProfileForm({ ...profileForm, exp_month: e.target.value })} />
                  <input autoComplete="cc-exp-year" placeholder="YYYY" inputMode="numeric" required value={profileForm.exp_year} onChange={(e) => setProfileForm({ ...profileForm, exp_year: e.target.value })} />
                  <input autoComplete="cc-csc" placeholder="CVV" inputMode="numeric" type="password" required value={profileForm.cvv} onChange={(e) => setProfileForm({ ...profileForm, cvv: e.target.value })} />
                </div>
                <input autoComplete="cc-name" placeholder="Name on card (optional)" value={profileForm.name_on_card} onChange={(e) => setProfileForm({ ...profileForm, name_on_card: e.target.value })} />
                <small className="secure-note">Card number/expiration/CVV are saved through the OS credential vault (Windows Credential Manager on Windows), not SQLite. They are never returned by the API.</small>
                <button className="primary-button" disabled={busy === "create-profile"}>Save Profile</button>
              </form>
            )}

            <div className="profile-list">
              {data.profiles.map((profile) => (
                <div className="profile-card" key={profile.id}>
                  <div className="avatar">{profile.name.slice(0, 2).toUpperCase()}</div>
                  <div>
                    <strong>{profile.name}</strong>
                    <small>{[profile.first_name, profile.last_name].filter(Boolean).join(" ") || profile.email || "Contact profile"}</small>
                    <small>
                      {profile.payment_vault_saved
                        ? (profile.payment_last4 ? `Vault saved •••• ${profile.payment_last4}` : "Vault saved")
                        : profile.payment_vault_error
                          ? "Payment vault unavailable"
                          : profile.payment_last4
                            ? `Vault missing — save card again (metadata •••• ${profile.payment_last4})`
                            : "No secure payment in vault"}
                    </small>
                  </div>
                  <div className="row-actions">
                    <button
                      className="mini-button"
                      onClick={() => {
                        setPaymentProfileId(profile.id);
                        setPaymentForm(emptyPayment);
                      }}
                    >
                      {profile.payment_vault_saved ? "Replace card" : "Add card"}
                    </button>
                    <button
                      className="icon-button danger"
                      onClick={() => runAction(`profile-${profile.id}`, `/api/profiles/${profile.id}`, { method: "DELETE" })}
                    >
                      ×
                    </button>
                  </div>
                </div>
              ))}
              {!data.profiles.length && <p className="empty-copy">No profiles yet.</p>}
            </div>

            {paymentProfileId && (
              <form className="profile-form" onSubmit={saveProfilePayment}>
                <div className="payment-divider">
                  Secure payment for {data.profiles.find((profile) => profile.id === Number(paymentProfileId))?.name || "profile"}
                </div>
                <input
                  autoComplete="cc-number"
                  placeholder="Card number"
                  inputMode="numeric"
                  type="password"
                  required
                  value={paymentForm.card_number}
                  onChange={(e) => setPaymentForm((previous) => ({ ...previous, card_number: e.target.value }))}
                />
                <div className="split-inputs three">
                  <input autoComplete="cc-exp-month" placeholder="MM" inputMode="numeric" required value={paymentForm.exp_month} onChange={(e) => setPaymentForm((previous) => ({ ...previous, exp_month: e.target.value }))} />
                  <input autoComplete="cc-exp-year" placeholder="YYYY" inputMode="numeric" required value={paymentForm.exp_year} onChange={(e) => setPaymentForm((previous) => ({ ...previous, exp_year: e.target.value }))} />
                  <input autoComplete="cc-csc" placeholder="CVV" inputMode="numeric" type="password" required value={paymentForm.cvv} onChange={(e) => setPaymentForm((previous) => ({ ...previous, cvv: e.target.value }))} />
                </div>
                <input autoComplete="cc-name" placeholder="Name on card (optional)" value={paymentForm.name_on_card} onChange={(e) => setPaymentForm((previous) => ({ ...previous, name_on_card: e.target.value }))} />
                <small className="secure-note">This replaces the payment secret for this profile in the OS credential vault.</small>
                <div className="row-actions">
                  <button className="primary-button" disabled={busy === `payment-${paymentProfileId}`}>Save Payment</button>
                  <button type="button" className="ghost-button" onClick={() => { setPaymentProfileId(null); setPaymentForm(emptyPayment); }}>Cancel</button>
                </div>
              </form>
            )}
          </section>

          <section className="panel activity-panel">
            <div className="panel-header compact">
              <div>
                <h2>Activity</h2>
                <p>Latest monitor and checkout events.</p>
              </div>
              <span className="count-pill">{data.events.length}</span>
            </div>
            <div className="event-list">
              {data.events.slice(0, 24).map((event) => (
                <div className="event-row" key={event.id}>
                  <span className={`event-dot ${event.level.toLowerCase()}`} />
                  <div>
                    <strong>{event.task_name || event.event_type}</strong>
                    <p>{event.message}</p>
                  </div>
                  <time>{new Date(event.created_at).toLocaleTimeString()}</time>
                </div>
              ))}
            </div>
          </section>
        </div>

        <section className="panel activity-panel">
          <div className="panel-header compact">
            <div>
              <h2>Checkout Runs</h2>
              <p>Each stock transition creates one prepared checkout run.</p>
            </div>
            <span className="count-pill">{data.checkout_runs?.length || 0}</span>
          </div>
          <div className="event-list">
            {(data.checkout_runs || []).slice(0, 12).map((run) => (
              <div className="event-row" key={run.id}>
                <span className={`event-dot ${["SUCCESS", "CARTED"].includes(run.status) ? "success" : "info"}`} />
                <div>
                  <strong>{run.task_name || `Checkout #${run.id}`}</strong>
                  <p>{run.status} · {run.store_name || run.store_id || "store pending"}</p>
                </div>
                <time>{new Date(run.created_at).toLocaleTimeString()}</time>
              </div>
            ))}
          </div>
        </section>

        <footer>
          <span>{monitors.length} monitors · {checkouts.length} checkout watchers · v10 CDP mode · hard-stops before Place Order</span>
          <span>Last engine check: {engine.last_checked_at ? new Date(engine.last_checked_at).toLocaleString() : "not yet"}</span>
        </footer>
      </main>
    </div>
  );
}

export default App;
