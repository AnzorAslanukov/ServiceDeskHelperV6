import warnings
warnings.filterwarnings("ignore")
from src.routers.frontend import templates

class FakeUser:
    display_name = "Test User"

class FakeState:
    user = FakeUser()

class FakeURL:
    path = "/ui/search"

class FakeRequest:
    state = FakeState()
    url = FakeURL()
    # Jinja2Templates needs request in context; url_for may be called
    def url_for(self, name, **kw):
        return "/"

# Render the child template block directly via the environment (bypass TemplateResponse/starlette)
env = templates.env
tpl = env.get_template("search/index.html")
html = tpl.render(request=FakeRequest(), active_page="search")
print("LEN", len(html))
print("has_legend", "operator-legend" in html)
print("has_summary", "Operator guide" in html)
print("has_eq_code", "<code>eq</code>" in html)
print("has_lt_code", "<code>lt</code>" in html)
print("has_note", "format-tolerant" in html)
i = html.find("operator-legend")
print("--- SNIPPET ---")
print(html[i-30:i+420])
