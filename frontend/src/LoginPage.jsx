import React, { useState } from "react";
import { api } from "./api";

export default function LoginPage({ onLogin, needsSetup }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      let result;
      if (needsSetup) {
        result = await api.setup(username, password, displayName || username);
      } else {
        result = await api.login(username, password);
      }
      api.setToken(result.token);
      onLogin(result);
    } catch (err) {
      setError(err.message);
    }
    setLoading(false);
  }

  return (
    <div style={st.backdrop}>
      <form onSubmit={handleSubmit} style={st.card} aria-label={needsSetup ? "Create admin account" : "Sign in"}>
        <div style={st.logo}>
          <img src="/logo.svg" alt="havn" width="48" height="48" style={{ marginBottom: "8px" }} />
          <div>havn</div>
        </div>
        <div style={st.subtitle}>
          {needsSetup ? "Create your admin account" : "Sign in to your data platform"}
        </div>

        {error && <div style={st.error} role="alert">{error}</div>}

        {needsSetup && (
          <div style={st.fieldGroup}>
            <label htmlFor="login-display-name" style={st.label}>Display Name</label>
            <input
              id="login-display-name"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="Your name"
              style={st.input}
            />
          </div>
        )}
        <div style={st.fieldGroup}>
          <label htmlFor="login-username" style={st.label}>Username</label>
          <input
            id="login-username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="Enter username"
            style={st.input}
            autoFocus
            aria-required="true"
          />
        </div>
        <div style={st.fieldGroup}>
          <label htmlFor="login-password" style={st.label}>Password</label>
          <input
            id="login-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Enter password"
            type="password"
            style={st.input}
            aria-required="true"
          />
        </div>
        <button type="submit" disabled={loading || !username || !password} style={st.btn} aria-label={needsSetup ? "Create account" : "Sign in"}>
          {loading ? "..." : needsSetup ? "Create Account" : "Sign In"}
        </button>
      </form>
    </div>
  );
}

const st = {
  backdrop: { display: "flex", alignItems: "center", justifyContent: "center", height: "100vh", background: "var(--havn-bg)", fontFamily: "var(--havn-font)" },
  card: { width: "360px", padding: "36px", background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius-lg)", display: "flex", flexDirection: "column", gap: "16px", boxShadow: "0 4px 24px rgba(0,0,0,0.15)" },
  logo: { display: "flex", flexDirection: "column", alignItems: "center", fontSize: "36px", fontWeight: 800, fontFamily: "var(--havn-font)", color: "var(--havn-accent)", textAlign: "center", letterSpacing: "-2px" },
  subtitle: { fontSize: "13px", color: "var(--havn-text-secondary)", textAlign: "center", marginBottom: "4px" },
  error: { padding: "8px 12px", background: "color-mix(in srgb, var(--havn-red) 12%, transparent)", border: "1px solid color-mix(in srgb, var(--havn-red) 30%, transparent)", borderRadius: "var(--havn-radius-lg)", color: "var(--havn-red)", fontSize: "12px" },
  fieldGroup: { display: "flex", flexDirection: "column", gap: "4px" },
  label: { fontSize: "11px", fontWeight: 600, color: "var(--havn-text-secondary)", textTransform: "uppercase", letterSpacing: "0.5px" },
  input: { padding: "10px 12px", background: "var(--havn-bg-tertiary)", border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius-lg)", color: "var(--havn-text)", fontSize: "14px" },
  btn: { padding: "11px", background: "var(--havn-green)", border: "1px solid var(--havn-green-border)", borderRadius: "var(--havn-radius-lg)", color: "#fff", cursor: "pointer", fontSize: "14px", fontWeight: 600, marginTop: "4px" },
};
