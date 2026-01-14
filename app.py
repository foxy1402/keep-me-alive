"""
Keep Me Alive - Streamlit Web Application
A service to keep your free-tier hosted apps alive by visiting them periodically.
Features: Admin authentication, GitHub Gist storage, Playwright browser automation.
"""
import streamlit as st
import os
from datetime import datetime
import time

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
    .stApp {
        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
    }
    
    .main-header {
        text-align: center;
        padding: 1rem 0;
        background: linear-gradient(90deg, #00d9ff, #00ff88);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        font-size: 3rem;
        font-weight: bold;
    }
    
    .login-container {
        max-width: 400px;
        margin: 100px auto;
        padding: 2rem;
        background: rgba(255, 255, 255, 0.05);
        border-radius: 20px;
        border: 1px solid rgba(255, 255, 255, 0.1);
    }
    
    .website-card {
        background: rgba(255, 255, 255, 0.08);
        border-radius: 10px;
        padding: 1rem;
        margin: 0.5rem 0;
        border-left: 4px solid #00d9ff;
    }
    
    .config-badge {
        background: linear-gradient(135deg, #00ff88, #00d9ff);
        color: #1a1a2e;
        padding: 0.25rem 0.75rem;
        border-radius: 20px;
        font-weight: bold;
        font-size: 0.75rem;
    }
    
    .warning-badge {
        background: linear-gradient(135deg, #ffaa00, #ff6b6b);
        color: #1a1a2e;
        padding: 0.25rem 0.75rem;
        border-radius: 20px;
        font-weight: bold;
        font-size: 0.75rem;
    }
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
    st.markdown('<h1 class="main-header">💓 Keep Me Alive</h1>', unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown("### 🔐 Admin Login")
        
        password = st.text_input("Password", type="password", key="login_password")
        
        if st.button("Login", type="primary", use_container_width=True):
            if password == ADMIN_PASSWORD:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Invalid password")
        
        if not ADMIN_PASSWORD:
            st.warning("⚠️ No ADMIN_PASSWORD set in environment. Access is open.")
            st.session_state.authenticated = True
            st.rerun()


def main_app():
    """Main application after authentication."""
    # Header
    st.markdown('<h1 class="main-header">💓 Keep Me Alive</h1>', unsafe_allow_html=True)
    st.markdown(
        '<p style="text-align: center; color: #888; margin-bottom: 2rem;">'
        'Keep your free-tier hosted apps alive by visiting them periodically'
        '</p>',
        unsafe_allow_html=True
    )
    
    # Show storage status
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        if is_gist_configured():
            st.markdown(
                '<p style="text-align: center;">'
                '<span class="config-badge">✅ Gist Storage Active</span>'
                '</p>',
                unsafe_allow_html=True
            )
        else:
            st.markdown(
                '<p style="text-align: center;">'
                '<span class="warning-badge">⚠️ Local Storage Only</span>'
                '</p>',
                unsafe_allow_html=True
            )
    
    # Initialize scheduler on app start
    if 'scheduler_initialized' not in st.session_state:
        start_scheduler()
        st.session_state.scheduler_initialized = True
    
    # Sidebar
    with st.sidebar:
        # Logout button
        if ADMIN_PASSWORD:
            if st.button("🚪 Logout", use_container_width=True):
                st.session_state.authenticated = False
                st.rerun()
        
        st.divider()
        st.header("⚙️ Settings")
        
        settings = get_settings()
        
        # Scheduler Status
        scheduler_status = get_scheduler_status()
        status_color = "🟢" if scheduler_status["running"] else "🔴"
        st.markdown(f"### {status_color} Scheduler")
        
        if scheduler_status["running"]:
            st.success("Running")
            if scheduler_status["next_run"]:
                next_run = datetime.fromisoformat(scheduler_status["next_run"])
                time_until = (next_run - datetime.now()).total_seconds() / 60
                st.info(f"Next: {max(0, time_until):.1f} min")
        else:
            st.warning("Stopped")
        
        col1, col2 = st.columns(2)
        with col1:
            if st.button("▶️ Start", use_container_width=True):
                start_scheduler()
                st.rerun()
        with col2:
            if st.button("⏹️ Stop", use_container_width=True):
                stop_scheduler()
                st.rerun()
        
        st.divider()
        
        # Interval Settings
        st.subheader("⏱️ Interval")
        col1, col2 = st.columns(2)
        with col1:
            new_min = st.number_input("Min", 1, 60, settings.get("interval_min", 10))
        with col2:
            new_max = st.number_input("Max", 1, 60, settings.get("interval_max", 14))
        
        if new_min <= new_max:
            if new_min != settings.get("interval_min") or new_max != settings.get("interval_max"):
                update_settings(interval_min=new_min, interval_max=new_max)
        
        st.divider()
        
        # Screenshot toggle
        screenshots_enabled = st.toggle(
            "📸 Screenshots",
            value=settings.get("screenshots_enabled", False)
        )
        if screenshots_enabled != settings.get("screenshots_enabled"):
            update_settings(screenshots_enabled=screenshots_enabled)
        
        st.divider()
        
        # Refresh data from Gist
        if is_gist_configured():
            if st.button("🔄 Sync from Gist", use_container_width=True):
                refresh_cache()
                st.rerun()
    
    # Main content
    tab1, tab2, tab3 = st.tabs(["📋 Websites", "📊 History", "ℹ️ About"])
    
    with tab1:
        # Add website
        st.subheader("➕ Add Website")
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
        
        st.divider()
        
        # Website list
        websites = get_websites()
        
        if not websites:
            st.info("No websites yet. Add one above! 👆")
        else:
            col1, col2 = st.columns([3, 1])
            with col2:
                if st.button("🚀 Visit All", type="secondary", use_container_width=True):
                    with st.spinner("Visiting..."):
                        enabled = [w for w in websites if w.get("enabled", True)]
                        if enabled:
                            results = visit_all_websites_sync(enabled)
                            ok = sum(1 for r in results if r["success"])
                            st.success(f"{ok}/{len(results)} OK")
                    st.rerun()
            
            for site in websites:
                col1, col2, col3, col4 = st.columns([0.5, 4, 1, 1])
                
                with col1:
                    enabled = st.checkbox("", value=site.get("enabled", True), key=f"en_{site['id']}", label_visibility="collapsed")
                    if enabled != site.get("enabled", True):
                        toggle_website(site["id"])
                        st.rerun()
                
                with col2:
                    icon = "✅" if site.get("enabled") else "⏸️"
                    st.markdown(f"""
                    <div class="website-card">
                        <strong>{icon} {site.get('name', site['url'])}</strong><br>
                        <small style="color: #888;">{site['url']}</small>
                    </div>
                    """, unsafe_allow_html=True)
                
                with col3:
                    if st.button("👁️", key=f"v_{site['id']}", use_container_width=True):
                        with st.spinner("..."):
                            ok, ms, err, _ = visit_website_sync(site['url'], get_settings().get("screenshots_enabled", False))
                            if ok:
                                st.success(f"✅ {ms:.0f}ms")
                            else:
                                st.error(f"❌")
                
                with col4:
                    if st.button("🗑️", key=f"d_{site['id']}", use_container_width=True):
                        remove_website(site["id"])
                        st.rerun()
    
    with tab2:
        col1, col2 = st.columns([4, 1])
        with col1:
            st.subheader("📊 Visit History")
        with col2:
            if st.button("🗑️ Clear", use_container_width=True):
                clear_visit_history()
                st.rerun()
        
        history = get_visit_history(30)
        
        if not history:
            st.info("No history yet.")
        else:
            for record in history:
                status = "✅" if record["success"] else "❌"
                ts = datetime.fromisoformat(record["timestamp"]).strftime("%m-%d %H:%M")
                url_short = record['url'][:40] + "..." if len(record['url']) > 40 else record['url']
                
                with st.expander(f"{status} {url_short} - {ts}"):
                    st.write(f"**URL:** {record['url']}")
                    st.write(f"**Time:** {record['response_time_ms']:.0f}ms")
                    if record.get("error_message"):
                        st.error(record['error_message'])
    
    with tab3:
        st.subheader("ℹ️ About")
        st.markdown("""
        **Keep Me Alive** prevents free-tier apps from sleeping.
        
        ### How it works
        1. Add website URLs
        2. Scheduler visits every 10-14 min
        3. Uses real browser (Playwright)
        
        ### Environment Variables
        | Variable | Description |
        |----------|-------------|
        | `ADMIN_PASSWORD` | Login password |
        | `GIST_TOKEN` | GitHub Personal Access Token |
        | `GIST_ID` | Gist ID for storage |
        
        ### Pro Tip 🤓
        Add this app's own URL to keep itself alive!
        """)


def main():
    if check_auth():
        main_app()
    else:
        login_page()


if __name__ == "__main__":
    main()
