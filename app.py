"""
Keep Me Alive - Streamlit Web Application
A service to keep your free-tier hosted apps alive by visiting them periodically.
Features: Admin authentication, GitHub Gist storage, Playwright browser automation.
"""
import streamlit as st
import os
import subprocess
import sys
from datetime import datetime
import time

# Install Playwright browsers on startup (needed for Streamlit Cloud)
@st.cache_resource
def install_playwright_browsers():
    """Install Playwright Chromium browser if not already installed."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=120
        )
        return result.returncode == 0
    except Exception as e:
        print(f"Playwright install error: {e}")
        return False

# Run installation
install_playwright_browsers()

from storage import (
    get_websites, add_website, remove_website, toggle_website,
    get_settings, update_settings, get_visit_history, clear_visit_history,
    refresh_cache
)
from scheduler import (
    start_scheduler, stop_scheduler, is_scheduler_running,
    get_scheduler_status, trigger_immediate_run
)
from browser_worker import visit_website_sync, visit_all_websites_sync
from gist_storage import is_gist_configured

# Page configuration
st.set_page_config(
    page_title="Keep Me Alive",
    page_icon="💓",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Get admin password from environment
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# Custom CSS
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

    html, body, [class*="css"], .stApp, [data-testid="stAppViewContainer"] {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    }

    .stApp {
        background:
            radial-gradient(1100px 550px at 8% -10%, rgba(34,211,238,0.10) 0%, transparent 55%),
            radial-gradient(900px 500px at 95% 0%, rgba(52,211,153,0.10) 0%, transparent 50%),
            linear-gradient(160deg, #0b1120 0%, #0d1526 55%, #0b1324 100%);
    }

    /* Hide default Streamlit chrome for a cleaner app feel */
    #MainMenu, header, footer { visibility: hidden; }

    .block-container { padding-top: 2.2rem; max-width: 1150px; }

    /* Header */
    .app-header { text-align: center; margin-bottom: 1.4rem; }
    .app-title {
        font-size: 2.6rem; font-weight: 800; letter-spacing: -0.03em; line-height: 1.1;
        background: linear-gradient(90deg, #22d3ee 0%, #34d399 100%);
        -webkit-background-clip: text; background-clip: text;
        -webkit-text-fill-color: transparent;
    }
    .app-sub { color: #94a3b8; margin-top: .35rem; font-size: 1rem; }

    /* Metric cards */
    [data-testid="stMetric"] {
        background: rgba(255,255,255,0.04);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 16px;
        padding: 1rem 1.2rem;
    }
    [data-testid="stMetricLabel"] p { color: #94a3b8; font-weight: 500; }
    [data-testid="stMetricValue"] { font-weight: 700; }

    /* Bordered containers as cards */
    [data-testid="stVerticalBlockBorderWrapper"] {
        background: rgba(255,255,255,0.03);
        border-radius: 16px;
    }

    /* Buttons */
    .stButton > button {
        border-radius: 10px;
        border: 1px solid rgba(255,255,255,0.12);
        font-weight: 600;
        transition: transform .12s ease, border-color .12s ease, background .12s ease;
    }
    .stButton > button:hover {
        transform: translateY(-1px);
        border-color: rgba(34,211,238,0.7);
    }

    /* Inputs */
    .stTextInput input, .stNumberInput input { border-radius: 10px; }

    /* Tabs */
    .stTabs [data-baseweb="tab-list"] { gap: .35rem; }
    .stTabs [data-baseweb="tab"] { border-radius: 10px 10px 0 0; padding: .4rem 1rem; }

    /* Pills */
    .pill {
        display:inline-block; padding: .25rem .8rem; border-radius: 999px;
        font-size: .78rem; font-weight: 600;
    }
    .pill-ok { background: rgba(52,211,153,0.15); color:#34d399; border:1px solid rgba(52,211,153,0.35); }
    .pill-warn { background: rgba(251,191,36,0.15); color:#fbbf24; border:1px solid rgba(251,191,36,0.35); }

    .site-name { font-weight: 600; font-size: 1.02rem; color: #e2e8f0; }
    .site-url { color: #64748b; font-size: .82rem; word-break: break-all; }
</style>
""", unsafe_allow_html=True)


def check_auth() -> bool:
    """Check if user is authenticated."""
    # If no password set, allow access (for local dev)
    if not ADMIN_PASSWORD:
        return True
    return st.session_state.get("authenticated", False)


def login_page():
    """Display login page."""
    st.markdown(
        '<div class="app-header">'
        '<span class="app-title">Keep Me Alive</span>'
        '<p class="app-sub">Keep your free-tier apps awake, automatically</p>'
        '</div>',
        unsafe_allow_html=True
    )

    col1, col2, col3 = st.columns([1, 1.3, 1])
    with col2:
        with st.container(border=True):
            st.markdown("#### Admin Login")

            # Use form for Enter key support
            with st.form("login_form"):
                password = st.text_input("Password", type="password", key="login_password")
                submitted = st.form_submit_button("Login", type="primary", use_container_width=True)

                if submitted:
                    if password == ADMIN_PASSWORD:
                        st.session_state.authenticated = True
                        st.rerun()
                    else:
                        st.error("Invalid password")

            if not ADMIN_PASSWORD:
                st.warning("No ADMIN_PASSWORD set in environment. Access is open.")
                st.session_state.authenticated = True
                st.rerun()


def _visit_stats(history: list):
    """Compute 24h success rate and check count from history."""
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(hours=24)
    recent = []
    for r in history:
        try:
            if datetime.fromisoformat(r.get("timestamp", "")) > cutoff:
                recent.append(r)
        except (ValueError, TypeError):
            continue
    ok = sum(1 for r in recent if r.get("success"))
    rate = f"{(ok / len(recent) * 100):.0f}%" if recent else "--"
    return rate, len(recent)


def main_app():
    """Main application after authentication."""
    # Initialize scheduler on app start
    if 'scheduler_initialized' not in st.session_state:
        start_scheduler()
        st.session_state.scheduler_initialized = True

    # Header
    st.markdown(
        '<div class="app-header">'
        '<span class="app-title">Keep Me Alive</span>'
        '<p class="app-sub">Keep your free-tier hosted apps alive with periodic real-browser visits</p>'
        '</div>',
        unsafe_allow_html=True
    )

    # Storage status pill
    if is_gist_configured():
        storage_pill = '<span class="pill pill-ok">Gist storage active</span>'
    else:
        storage_pill = '<span class="pill pill-warn">Local storage only</span>'
    st.markdown(
        f'<div style="text-align:center; margin-bottom:1.2rem;">{storage_pill}</div>',
        unsafe_allow_html=True
    )

    # Load data + compute metrics
    websites = get_websites()
    history = get_visit_history(100)
    scheduler_status = get_scheduler_status()

    total = len(websites)
    active = sum(1 for w in websites if w.get("enabled", True))
    rate, checks_24h = _visit_stats(history)

    if scheduler_status["running"] and scheduler_status["next_run"]:
        mins = (datetime.fromisoformat(scheduler_status["next_run"]) - datetime.now()).total_seconds() / 60
        next_run_display = f"{max(0, mins):.0f} min"
    elif scheduler_status["running"]:
        next_run_display = "soon"
    else:
        next_run_display = "stopped"

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Sites", total)
    m2.metric("Active", active)
    m3.metric("Success (24h)", rate, f"{checks_24h} checks", delta_color="off")
    m4.metric("Next Visit", next_run_display)

    st.write("")

    # Sidebar
    with st.sidebar:
        st.markdown("### Controls")

        if ADMIN_PASSWORD:
            if st.button("Logout", use_container_width=True):
                st.session_state.authenticated = False
                st.rerun()
            st.divider()

        settings = get_settings()

        status_icon = "🟢" if scheduler_status["running"] else "🔴"
        status_text = "Running" if scheduler_status["running"] else "Stopped"
        st.markdown(f"**{status_icon} Scheduler** — {status_text}")

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Start", use_container_width=True):
                start_scheduler()
                st.rerun()
        with col2:
            if st.button("Stop", use_container_width=True):
                stop_scheduler()
                st.rerun()

        st.divider()
        st.markdown("**Interval (minutes)**")
        col1, col2 = st.columns(2)
        with col1:
            new_min = st.number_input("Min", 1, 60, settings.get("interval_min", 10))
        with col2:
            new_max = st.number_input("Max", 1, 60, settings.get("interval_max", 14))

        if new_min <= new_max:
            if new_min != settings.get("interval_min") or new_max != settings.get("interval_max"):
                update_settings(interval_min=new_min, interval_max=new_max)
        else:
            st.caption("Min must be less than or equal to Max")

        st.divider()
        screenshots_enabled = st.toggle(
            "Capture screenshots",
            value=settings.get("screenshots_enabled", False)
        )
        if screenshots_enabled != settings.get("screenshots_enabled"):
            update_settings(screenshots_enabled=screenshots_enabled)

        if is_gist_configured():
            st.divider()
            if st.button("Sync from Gist", use_container_width=True):
                refresh_cache()
                st.rerun()

    # Main content
    tab1, tab2, tab3 = st.tabs(["Websites", "History", "About"])

    with tab1:
        with st.container(border=True):
            st.markdown("**Add a website**")
            col1, col2, col3 = st.columns([3, 2, 1])
            with col1:
                new_url = st.text_input("URL", placeholder="https://your-app.onrender.com", label_visibility="collapsed")
            with col2:
                new_name = st.text_input("Name", placeholder="My App", label_visibility="collapsed")
            with col3:
                if st.button("Add", type="primary", use_container_width=True):
                    if new_url:
                        if not new_url.startswith(("http://", "https://")):
                            new_url = "https://" + new_url
                        if add_website(new_url, new_name):
                            st.success("Added!")
                            st.rerun()
                        else:
                            st.error("Already exists!")

        st.write("")

        if not websites:
            st.info("No websites yet. Add one above to get started.")
        else:
            head1, head2 = st.columns([3, 1])
            with head1:
                st.markdown(f"**Your sites** ({active}/{total} active)")
            with head2:
                if st.button("Visit all now", type="secondary", use_container_width=True):
                    with st.spinner("Visiting all websites..."):
                        enabled = [w for w in websites if w.get("enabled", True)]
                        if enabled:
                            results = visit_all_websites_sync(enabled)
                            ok = sum(1 for r in results if r["success"])
                            st.success(f"{ok}/{len(results)} completed")
                            for r in results:
                                icon = "✅" if r["success"] else "❌"
                                st.write(f"{icon} **{r['name']}** — {r['response_time_ms']:.0f} ms")
                                if r.get("screenshot"):
                                    st.image(r["screenshot"], caption=r["name"], use_container_width=True)
                                if r.get("error"):
                                    st.error(r["error"])

            for site in websites:
                with st.container(border=True):
                    c1, c2, c3, c4 = st.columns([0.7, 5, 1.2, 1.2])

                    with c1:
                        enabled = st.toggle(
                            f"Toggle {site['id']}",
                            value=site.get("enabled", True),
                            key=f"en_{site['id']}",
                            label_visibility="collapsed"
                        )
                        if enabled != site.get("enabled", True):
                            toggle_website(site["id"])
                            st.rerun()

                    with c2:
                        st.markdown(
                            f'<div class="site-name">{site.get("name", site["url"])}</div>'
                            f'<div class="site-url">{site["url"]}</div>',
                            unsafe_allow_html=True
                        )

                    with c3:
                        if st.button("Visit", key=f"v_{site['id']}", use_container_width=True):
                            with st.spinner("Visiting..."):
                                take_ss = get_settings().get("screenshots_enabled", False)
                                ok, ms, err, screenshot = visit_website_sync(site['url'], take_ss)
                                if ok:
                                    st.success(f"{ms:.0f} ms")
                                    if screenshot:
                                        st.image(screenshot, caption=site['name'], use_container_width=True)
                                else:
                                    st.error(f"{err[:60]}" if err else "Failed")

                    with c4:
                        if st.button("Delete", key=f"d_{site['id']}", use_container_width=True):
                            remove_website(site["id"])
                            st.rerun()

    with tab2:
        head1, head2 = st.columns([4, 1])
        with head1:
            st.markdown("**Visit history**")
        with head2:
            if st.button("Clear", use_container_width=True):
                clear_visit_history()
                st.rerun()

        if not history:
            st.info("No history yet.")
        else:
            total_h = len(history)
            ok_h = sum(1 for r in history if r.get("success"))
            s1, s2, s3 = st.columns(3)
            s1.metric("Recorded", total_h)
            s2.metric("Successful", ok_h)
            s3.metric("Failed", total_h - ok_h)
            st.divider()

            for record in history[:30]:
                icon = "✅" if record["success"] else "❌"
                ts = datetime.fromisoformat(record["timestamp"]).strftime("%b %d, %H:%M")
                url_short = record['url'][:45] + "..." if len(record['url']) > 45 else record['url']

                with st.expander(f"{icon}  {url_short}  ·  {ts}"):
                    st.write(f"**URL:** {record['url']}")
                    st.write(f"**Response time:** {record['response_time_ms']:.0f} ms")
                    if record.get("error_message"):
                        st.error(record['error_message'])

    with tab3:
        with st.container(border=True):
            st.markdown("""
            #### About Keep Me Alive
            Prevents free-tier apps from sleeping by visiting them with a real browser on a randomized schedule.

            **How it works**
            1. Add your website URLs
            2. The scheduler visits them every few minutes (randomized interval)
            3. Each visit uses a real Chromium browser (via Playwright)

            **Environment variables**

            | Variable | Description |
            |----------|-------------|
            | `ADMIN_PASSWORD` | Login password |
            | `GIST_TOKEN` | GitHub Personal Access Token |
            | `GIST_ID` | Gist ID for storage |
            | `PORT` | Port to bind (default 8501) |

            **Tip:** add this app's own URL to keep itself alive.
            """)


def main():
    if check_auth():
        main_app()
    else:
        login_page()


if __name__ == "__main__":
    main()
