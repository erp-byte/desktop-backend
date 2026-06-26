"""Generate Auth flow + payload reference PDF for the Candor Consumption backend.

Source of truth for content:
- Plan/Frontend/1_Authentication_and_Authorization.md
- modules/auth/router.py
- modules/auth/schemas.py

Run:
    C:\\Python314\\python.exe scripts\\gen_auth_flow_pdf.py
Output:
    docs/AUTH_AUTHZ_FLOW.pdf
"""

from __future__ import annotations

import os
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)


OUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "AUTH_AUTHZ_FLOW.pdf"
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)


# ─── Styles ───────────────────────────────────────────────────────────────

styles = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=18, spaceAfter=8, textColor=colors.HexColor("#1a2b4a"))
H2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=13, spaceAfter=4, spaceBefore=10, textColor=colors.HexColor("#1a2b4a"))
H3 = ParagraphStyle("H3", parent=styles["Heading3"], fontSize=11, spaceAfter=3, spaceBefore=6, textColor=colors.HexColor("#39507a"))
BODY = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=9, leading=12)
SMALL = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=8, leading=10)
CODE = ParagraphStyle("Code", parent=styles["Code"], fontSize=7.5, leading=9.5, leftIndent=4, textColor=colors.HexColor("#111"))
CAPTION = ParagraphStyle("Caption", parent=styles["BodyText"], fontSize=8, leading=10, alignment=TA_CENTER, textColor=colors.HexColor("#555"))


# ─── Flow diagram primitive ──────────────────────────────────────────────


class FlowDiagram(Flowable):
    """A custom flowable that draws a flow diagram on canvas.

    boxes:  list of dicts: {id, x, y, w, h, text, fill, stroke}
    arrows: list of dicts: {from, to, label?, side_from?, side_to?, bend?}
            sides: "n","s","e","w","auto"  — defaults to auto (closest edge)
    """

    def __init__(self, width: float, height: float, boxes, arrows, title: str = ""):
        super().__init__()
        self.width = width
        self.height = height
        self.boxes = {b["id"]: b for b in boxes}
        self.arrows = arrows
        self.title = title

    def wrap(self, *args):
        return self.width, self.height

    def _anchor(self, box, side):
        x, y, w, h = box["x"], box["y"], box["w"], box["h"]
        if side == "n":
            return (x + w / 2, y + h)
        if side == "s":
            return (x + w / 2, y)
        if side == "e":
            return (x + w, y + h / 2)
        if side == "w":
            return (x, y + h / 2)
        return (x + w / 2, y + h / 2)

    def _best_sides(self, a, b):
        ax, ay = a["x"] + a["w"] / 2, a["y"] + a["h"] / 2
        bx, by = b["x"] + b["w"] / 2, b["y"] + b["h"] / 2
        dx, dy = bx - ax, by - ay
        if abs(dx) > abs(dy):
            return ("e", "w") if dx > 0 else ("w", "e")
        return ("n", "s") if dy > 0 else ("s", "n")

    def draw(self):
        c = self.canv
        # Optional title bar
        if self.title:
            c.setFont("Helvetica-Bold", 10)
            c.setFillColor(colors.HexColor("#1a2b4a"))
            c.drawString(0, self.height - 12, self.title)

        # Boxes
        for b in self.boxes.values():
            x, y, w, h = b["x"], b["y"], b["w"], b["h"]
            fill = colors.HexColor(b.get("fill", "#eaf1fb"))
            stroke = colors.HexColor(b.get("stroke", "#2c5aa0"))
            shape = b.get("shape", "rect")
            c.setStrokeColor(stroke)
            c.setLineWidth(0.8)
            c.setFillColor(fill)
            if shape == "diamond":
                p = c.beginPath()
                p.moveTo(x + w / 2, y + h)
                p.lineTo(x + w, y + h / 2)
                p.lineTo(x + w / 2, y)
                p.lineTo(x, y + h / 2)
                p.close()
                c.drawPath(p, fill=1, stroke=1)
            elif shape == "round":
                c.roundRect(x, y, w, h, 5, fill=1, stroke=1)
            else:
                c.rect(x, y, w, h, fill=1, stroke=1)

            # Text
            c.setFillColor(colors.HexColor(b.get("text_color", "#111")))
            text_lines = b["text"].split("\n")
            font = b.get("font", "Helvetica")
            font_size = b.get("font_size", 7.5)
            c.setFont(font, font_size)
            line_h = font_size + 1.2
            total_text_h = line_h * len(text_lines)
            start_y = y + h / 2 + total_text_h / 2 - line_h
            for i, line in enumerate(text_lines):
                c.drawCentredString(x + w / 2, start_y - i * line_h + line_h / 4, line)

        # Arrows
        for a in self.arrows:
            src = self.boxes[a["from"]]
            dst = self.boxes[a["to"]]
            sf = a.get("side_from")
            st = a.get("side_to")
            if not sf or not st:
                auto_sf, auto_st = self._best_sides(src, dst)
                sf = sf or auto_sf
                st = st or auto_st
            x1, y1 = self._anchor(src, sf)
            x2, y2 = self._anchor(dst, st)
            color = colors.HexColor(a.get("color", "#2c5aa0"))
            c.setStrokeColor(color)
            c.setFillColor(color)
            c.setLineWidth(0.8)

            if a.get("bend") == "L":
                # Right-angle bend via a midpoint
                mid_x, mid_y = x2, y1
                c.line(x1, y1, mid_x, mid_y)
                c.line(mid_x, mid_y, x2, y2)
            else:
                c.line(x1, y1, x2, y2)
            # Arrow head
            import math
            ang = math.atan2(y2 - y1, x2 - x1)
            ah = 4.5
            c.setLineWidth(0.5)
            p = c.beginPath()
            p.moveTo(x2, y2)
            p.lineTo(x2 - ah * math.cos(ang - math.pi / 7), y2 - ah * math.sin(ang - math.pi / 7))
            p.lineTo(x2 - ah * math.cos(ang + math.pi / 7), y2 - ah * math.sin(ang + math.pi / 7))
            p.close()
            c.drawPath(p, fill=1, stroke=0)

            # Label
            label = a.get("label")
            if label:
                lx, ly = (x1 + x2) / 2, (y1 + y2) / 2
                if a.get("bend") == "L":
                    lx, ly = x2, y1
                c.setFont("Helvetica", 6.5)
                c.setFillColor(colors.HexColor("#333"))
                # Label background
                text_w = c.stringWidth(label, "Helvetica", 6.5)
                c.setFillColor(colors.HexColor("#ffffff"))
                c.rect(lx - text_w / 2 - 1.5, ly - 3.5, text_w + 3, 7.5, fill=1, stroke=0)
                c.setFillColor(colors.HexColor("#333"))
                c.drawCentredString(lx, ly - 2, label)


# ─── Diagram builders ─────────────────────────────────────────────────────


def diagram_high_level():
    """High-level architecture: clients ↔ FastAPI auth router ↔ services ↔ DB."""
    W, H = 470, 240
    blue = "#eaf1fb"
    green = "#e8f5e8"
    yellow = "#fff7d6"
    orange = "#fde2c4"
    red = "#fbe1e1"

    boxes = [
        {"id": "el", "x": 10, "y": 175, "w": 110, "h": 40,
         "text": "Electron Desktop\n(React + axios)\nsafeStorage + electron-store", "fill": blue, "stroke": "#2c5aa0",
         "shape": "round"},
        {"id": "an", "x": 10, "y": 110, "w": 110, "h": 40,
         "text": "Android Java app\n(Retrofit + OkHttp)\nEncryptedSharedPreferences", "fill": blue, "stroke": "#2c5aa0",
         "shape": "round"},
        {"id": "lb", "x": 160, "y": 145, "w": 90, "h": 35,
         "text": "HTTPS\nBearer <access_token>\nrefresh_token in body", "fill": "#f4f4f4", "stroke": "#888",
         "shape": "round", "font_size": 6.8},
        {"id": "fa", "x": 285, "y": 175, "w": 170, "h": 50,
         "text": "FastAPI router\n/api/v1/auth/*\nrequest_context middleware:\nerror envelope, X-Request-ID, no-store",
         "fill": yellow, "stroke": "#b88a00", "shape": "round"},
        {"id": "mw", "x": 285, "y": 115, "w": 170, "h": 40,
         "text": "AuthUser dependency (middleware.py)\nvalidate JWT access, build user ctx\nRBAC: has_perm(module, sub, action)",
         "fill": orange, "stroke": "#b86c00", "shape": "round"},
        {"id": "sv", "x": 285, "y": 55, "w": 170, "h": 45,
         "text": "Services\nauth_service · jwt_service\npassword_rules · phone · rate_limiter\npermission_service",
         "fill": green, "stroke": "#2d7a3d", "shape": "round"},
        {"id": "db", "x": 285, "y": 5, "w": 170, "h": 30,
         "text": "PostgreSQL (asyncpg pool)\nauth_user · auth_role · auth_permission\nauth_role_permission · auth_refresh_token",
         "fill": red, "stroke": "#a04444", "shape": "round", "font_size": 6.8},
    ]
    arrows = [
        {"from": "el", "to": "fa", "side_from": "e", "side_to": "w", "label": "HTTPS"},
        {"from": "an", "to": "fa", "side_from": "e", "side_to": "w", "label": "HTTPS"},
        {"from": "fa", "to": "mw", "side_from": "s", "side_to": "n", "label": "Depends(get_current_user)"},
        {"from": "mw", "to": "sv", "side_from": "s", "side_to": "n", "label": "call"},
        {"from": "sv", "to": "db", "side_from": "s", "side_to": "n", "label": "asyncpg"},
    ]
    return FlowDiagram(W, H, boxes, arrows, title="Figure 1 — End-to-end architecture")


def diagram_login_flow():
    W, H = 470, 540
    blue = "#eaf1fb"; green = "#e8f5e8"; yellow = "#fff7d6"; red = "#fbe1e1"; diamond = "#fde2c4"

    boxes = [
        {"id": "u",  "x": 180, "y": 510, "w": 120, "h": 25,
         "text": "User taps Sign In", "fill": blue, "stroke": "#2c5aa0", "shape": "round"},
        {"id": "c",  "x": 130, "y": 458, "w": 220, "h": 38,
         "text": "Client POST /api/v1/auth/login\n{ phone, password, device_info? }\nNo Authorization header",
         "fill": blue, "stroke": "#2c5aa0"},
        {"id": "rl", "x": 130, "y": 410, "w": 220, "h": 28,
         "text": "rate_limiter.check_and_record(ip, phone)", "fill": yellow, "stroke": "#b88a00"},
        {"id": "rl_d", "x": 380, "y": 410, "w": 75, "h": 28,
         "text": "429\nrate_limit_exceeded\n(Retry-After)", "fill": red, "stroke": "#a04444",
         "font_size": 6.5},
        {"id": "np", "x": 130, "y": 372, "w": 220, "h": 28,
         "text": "phone.normalize → +91XXXXXXXXXX", "fill": yellow, "stroke": "#b88a00"},
        {"id": "lk", "x": 130, "y": 320, "w": 220, "h": 40,
         "text": "auth_service.login(conn, …)\n• fetch user by normalized phone\n• check status / locked_until / is_active",
         "fill": yellow, "stroke": "#b88a00"},
        {"id": "lk_d", "x": 380, "y": 320, "w": 75, "h": 40,
         "text": "423 account_locked\n403 account_suspended\n403 account_disabled",
         "fill": red, "stroke": "#a04444", "font_size": 6.2},
        {"id": "vp", "x": 165, "y": 268, "w": 150, "h": 40,
         "text": "verify_password(\nbcrypt hash)", "fill": diamond, "stroke": "#b86c00", "shape": "diamond"},
        {"id": "fail","x": 350, "y": 270, "w": 110, "h": 38,
         "text": "401 invalid_credentials\n+ UPDATE failed_login_count\n(NO transaction wrap)",
         "fill": red, "stroke": "#a04444", "font_size": 6.5},
        {"id": "is", "x": 130, "y": 210, "w": 220, "h": 40,
         "text": "jwt_service.issue_pair(user_id)\n• access JWT (HS256, exp=15m)\n• refresh JWT (HS256, exp=8h, jti)",
         "fill": green, "stroke": "#2d7a3d"},
        {"id": "st", "x": 130, "y": 162, "w": 220, "h": 38,
         "text": "INSERT auth_refresh_token\n(jti, user_id, expires_at, ip, ua, device_info,\nparent_jti=NULL — root of chain)",
         "fill": green, "stroke": "#2d7a3d"},
        {"id": "ll", "x": 130, "y": 122, "w": 220, "h": 28,
         "text": "UPDATE auth_user SET last_login_at=NOW(),\nfailed_login_count=0, locked_until=NULL",
         "fill": green, "stroke": "#2d7a3d", "font_size": 6.8},
        {"id": "rs", "x": 130, "y": 60, "w": 220, "h": 50,
         "text": "200 LoginResponse\n{ access_token, refresh_token, token_type:\"Bearer\",\nexpires_in, refresh_expires_in,\nmust_change_password, user{…, roles[]} }",
         "fill": blue, "stroke": "#2c5aa0", "font_size": 6.8},
        {"id": "cli", "x": 130, "y": 8, "w": 220, "h": 40,
         "text": "Client: encrypt + persist both tokens.\nif must_change_password → S2 Force-Change.\nElse: call GET /me, then route to home.",
         "fill": blue, "stroke": "#2c5aa0", "font_size": 7},
    ]
    arrows = [
        {"from": "u",  "to": "c",  "side_from": "s", "side_to": "n"},
        {"from": "c",  "to": "rl", "side_from": "s", "side_to": "n"},
        {"from": "rl", "to": "rl_d", "side_from": "e", "side_to": "w", "label": "limit"},
        {"from": "rl", "to": "np", "side_from": "s", "side_to": "n", "label": "ok"},
        {"from": "np", "to": "lk", "side_from": "s", "side_to": "n"},
        {"from": "lk", "to": "lk_d", "side_from": "e", "side_to": "w", "label": "blocked"},
        {"from": "lk", "to": "vp", "side_from": "s", "side_to": "n", "label": "ok"},
        {"from": "vp", "to": "fail","side_from": "e", "side_to": "w", "label": "no"},
        {"from": "vp", "to": "is", "side_from": "s", "side_to": "n", "label": "yes"},
        {"from": "is", "to": "st", "side_from": "s", "side_to": "n"},
        {"from": "st", "to": "ll", "side_from": "s", "side_to": "n"},
        {"from": "ll", "to": "rs", "side_from": "s", "side_to": "n"},
        {"from": "rs", "to": "cli","side_from": "s", "side_to": "n"},
    ]
    return FlowDiagram(W, H, boxes, arrows, title="Figure 2 — Login flow (POST /api/v1/auth/login)")


def diagram_protected_call_and_refresh():
    W, H = 470, 540
    blue = "#eaf1fb"; green = "#e8f5e8"; yellow = "#fff7d6"; red = "#fbe1e1"; diamond = "#fde2c4"

    boxes = [
        {"id": "ui", "x": 150, "y": 510, "w": 170, "h": 25,
         "text": "User action → protected API call", "fill": blue, "stroke": "#2c5aa0", "shape": "round"},
        {"id": "cli","x": 130, "y": 458, "w": 210, "h": 38,
         "text": "axios / OkHttp interceptor:\nAuthorization: Bearer <access_token>", "fill": blue, "stroke": "#2c5aa0"},
        {"id": "rt", "x": 130, "y": 410, "w": 210, "h": 28,
         "text": "FastAPI route Depends(get_current_user)", "fill": yellow, "stroke": "#b88a00"},
        {"id": "vt", "x": 165, "y": 355, "w": 140, "h": 38,
         "text": "jwt_service.verify\naccess token", "fill": diamond, "stroke": "#b86c00", "shape": "diamond"},
        {"id": "401","x": 330, "y": 358, "w": 130, "h": 32,
         "text": "401\ninvalid_access_token\nor token_expired",
         "fill": red, "stroke": "#a04444"},
        {"id": "lu", "x": 130, "y": 300, "w": 210, "h": 38,
         "text": "Load AuthUser ctx:\nuser_id, is_admin, roles, permissions[],\nentities/warehouses/floors",
         "fill": yellow, "stroke": "#b88a00", "font_size": 7},
        {"id": "rc", "x": 165, "y": 248, "w": 140, "h": 40,
         "text": "RBAC check\nhas_perm(module,\nsub_module, action)?",
         "fill": diamond, "stroke": "#b86c00", "shape": "diamond"},
        {"id": "403","x": 330, "y": 252, "w": 130, "h": 32,
         "text": "403 forbidden\ndetails: {module,\nsub_module, action}",
         "fill": red, "stroke": "#a04444"},
        {"id": "ok", "x": 130, "y": 200, "w": 210, "h": 32,
         "text": "Handler executes, returns 2xx",
         "fill": green, "stroke": "#2d7a3d"},

        # Refresh branch (left side)
        {"id": "ic", "x": 10,  "y": 295, "w": 110, "h": 50,
         "text": "Client interceptor\nsees 401 →\nsingle-flight refresh", "fill": blue, "stroke": "#2c5aa0",
         "font_size": 7},
        {"id": "rf", "x": 10,  "y": 232, "w": 110, "h": 54,
         "text": "POST /auth/refresh\n{ refresh_token }\nno Authorization header",
         "fill": blue, "stroke": "#2c5aa0", "font_size": 7},
        {"id": "vr", "x": 10,  "y": 165, "w": 110, "h": 58,
         "text": "Server:\n• verify JWT sig + exp\n• SELECT auth_refresh_token\n• reuse-detect on jti",
         "fill": yellow, "stroke": "#b88a00", "font_size": 6.8},
        {"id": "rev","x": 10,  "y": 100, "w": 110, "h": 56,
         "text": "If jti already revoked\n→ revoke whole chain\n→ 401 token_reuse_detected",
         "fill": red, "stroke": "#a04444", "font_size": 6.5},
        {"id": "rot","x": 10,  "y": 36,  "w": 110, "h": 56,
         "text": "Rotate:\n• issue new pair\n• mark old jti revoked\n• new row, parent_jti=old",
         "fill": green, "stroke": "#2d7a3d", "font_size": 6.8},
        {"id": "rty","x": 130, "y": 110, "w": 210, "h": 36,
         "text": "Client persists rotated pair atomically,\nretries original request ONCE with new access",
         "fill": blue, "stroke": "#2c5aa0", "font_size": 7},
        {"id": "fkl","x": 350, "y": 36,  "w": 110, "h": 56,
         "text": "Refresh 401 →\nclear tokens, emit\nforce-logout event,\nroute to /login",
         "fill": red, "stroke": "#a04444", "font_size": 6.8},
    ]
    arrows = [
        {"from": "ui", "to": "cli", "side_from": "s", "side_to": "n"},
        {"from": "cli","to": "rt",  "side_from": "s", "side_to": "n"},
        {"from": "rt", "to": "vt",  "side_from": "s", "side_to": "n"},
        {"from": "vt", "to": "401", "side_from": "e", "side_to": "w", "label": "invalid/expired"},
        {"from": "vt", "to": "lu",  "side_from": "s", "side_to": "n", "label": "valid"},
        {"from": "lu", "to": "rc",  "side_from": "s", "side_to": "n"},
        {"from": "rc", "to": "403", "side_from": "e", "side_to": "w", "label": "no"},
        {"from": "rc", "to": "ok",  "side_from": "s", "side_to": "n", "label": "yes"},
        # Refresh side
        {"from": "401","to": "ic",  "side_from": "w", "side_to": "n", "label": "401 → interceptor"},
        {"from": "ic", "to": "rf",  "side_from": "s", "side_to": "n"},
        {"from": "rf", "to": "vr",  "side_from": "s", "side_to": "n"},
        {"from": "vr", "to": "rev", "side_from": "s", "side_to": "n", "label": "reused jti"},
        {"from": "vr", "to": "rot", "side_from": "e", "side_to": "n", "label": "fresh"},
        {"from": "rot","to": "rty", "side_from": "e", "side_to": "w", "label": "200 new pair"},
        {"from": "rev","to": "fkl", "side_from": "e", "side_to": "w"},
        {"from": "rty","to": "rt",  "side_from": "n", "side_to": "w", "label": "retry once", "bend": "L"},
    ]
    return FlowDiagram(W, H, boxes, arrows, title="Figure 3 — Protected request + transparent refresh")


def diagram_logout_and_password():
    W, H = 470, 350
    blue = "#eaf1fb"; green = "#e8f5e8"; yellow = "#fff7d6"; red = "#fbe1e1"

    boxes = [
        # Logout column (left)
        {"id": "lo1", "x": 10, "y": 310, "w": 200, "h": 32,
         "text": "POST /auth/logout\n{ refresh_token }  +  Authorization Bearer", "fill": blue, "stroke": "#2c5aa0",
         "font_size": 7},
        {"id": "lo2", "x": 10, "y": 260, "w": 200, "h": 38,
         "text": "auth_service.logout(conn, refresh_jwt, user_id)\nrevoke ONLY if jti belongs to user (silent)\n(idempotent — always 204)",
         "fill": yellow, "stroke": "#b88a00", "font_size": 6.8},
        {"id": "lo3", "x": 10, "y": 218, "w": 200, "h": 28,
         "text": "Client clears local tokens", "fill": blue, "stroke": "#2c5aa0"},

        {"id": "la1", "x": 10, "y": 160, "w": 200, "h": 30,
         "text": "POST /auth/logout-all  (Bearer)", "fill": blue, "stroke": "#2c5aa0"},
        {"id": "la2", "x": 10, "y": 110, "w": 200, "h": 38,
         "text": "UPDATE auth_refresh_token\nSET revoked_at=NOW() WHERE user_id=$1\nAND revoked_at IS NULL",
         "fill": yellow, "stroke": "#b88a00", "font_size": 6.8},
        {"id": "la3", "x": 10, "y": 60,  "w": 200, "h": 38,
         "text": "200 { revoked_count: N }\nClient clears tokens, routes to /login",
         "fill": green, "stroke": "#2d7a3d", "font_size": 7},

        # Password change column (right)
        {"id": "pc1", "x": 250, "y": 310, "w": 210, "h": 32,
         "text": "POST /auth/password/change  (Bearer)\n{ old_password, new_password, confirm_password }",
         "fill": blue, "stroke": "#2c5aa0", "font_size": 7},
        {"id": "pc2", "x": 250, "y": 260, "w": 210, "h": 38,
         "text": "verify_password(old)\n→ 401 invalid_old_password on mismatch",
         "fill": yellow, "stroke": "#b88a00", "font_size": 7},
        {"id": "pc3", "x": 250, "y": 212, "w": 210, "h": 38,
         "text": "password_rules.evaluate(new, phone)\n→ 400 weak_password\ndetails.rules: ['length_12_128', …]",
         "fill": yellow, "stroke": "#b88a00", "font_size": 6.8},
        {"id": "pc4", "x": 250, "y": 165, "w": 210, "h": 38,
         "text": "if new != confirm → 400 password_mismatch\nUPDATE password_hash, password_changed_at,\nmust_change_password=FALSE",
         "fill": green, "stroke": "#2d7a3d", "font_size": 6.8},
        {"id": "pc5", "x": 250, "y": 110, "w": 210, "h": 46,
         "text": "Revoke EVERY OTHER refresh token for user\n(keep current jti alive) → revoke_reason='password_changed'",
         "fill": green, "stroke": "#2d7a3d", "font_size": 7},
        {"id": "pc6", "x": 250, "y": 60,  "w": 210, "h": 38,
         "text": "200 { message, revoked_count }\nClient toast: 'Other devices signed out (N)'",
         "fill": green, "stroke": "#2d7a3d", "font_size": 7},

        # Admin reset (bottom strip)
        {"id": "ad1", "x": 10, "y": 8, "w": 450, "h": 38,
         "text": "Admin reset (POST /auth/users/{id}/reset-password, Bearer admin) — { new_password } → forces must_change_password=TRUE on target,\nrevokes ALL target's refresh tokens, clears lockout. Same password policy as /password/change.",
         "fill": "#f6e1ff", "stroke": "#6b3aa0", "font_size": 7},
    ]
    arrows = [
        {"from": "lo1", "to": "lo2", "side_from": "s", "side_to": "n"},
        {"from": "lo2", "to": "lo3", "side_from": "s", "side_to": "n"},
        {"from": "lo3", "to": "la1", "side_from": "s", "side_to": "n"},
        {"from": "la1", "to": "la2", "side_from": "s", "side_to": "n"},
        {"from": "la2", "to": "la3", "side_from": "s", "side_to": "n"},
        {"from": "pc1", "to": "pc2", "side_from": "s", "side_to": "n"},
        {"from": "pc2", "to": "pc3", "side_from": "s", "side_to": "n"},
        {"from": "pc3", "to": "pc4", "side_from": "s", "side_to": "n"},
        {"from": "pc4", "to": "pc5", "side_from": "s", "side_to": "n"},
        {"from": "pc5", "to": "pc6", "side_from": "s", "side_to": "n"},
    ]
    return FlowDiagram(W, H, boxes, arrows, title="Figure 4 — Logout, logout-all, change-password, admin reset")


def diagram_authz():
    W, H = 470, 260
    blue = "#eaf1fb"; green = "#e8f5e8"; yellow = "#fff7d6"; red = "#fbe1e1"; diamond = "#fde2c4"

    boxes = [
        {"id": "rq", "x": 130, "y": 220, "w": 210, "h": 28,
         "text": "Authorized request reaches a guarded handler", "fill": blue, "stroke": "#2c5aa0"},
        {"id": "ld", "x": 130, "y": 172, "w": 210, "h": 38,
         "text": "permission_service.has_perm(user, module,\nsub_module, action [, sub_sub_module])", "fill": yellow,
         "stroke": "#b88a00", "font_size": 7},
        {"id": "ad", "x": 165, "y": 122, "w": 140, "h": 38,
         "text": "user.is_admin?", "fill": diamond, "stroke": "#b86c00", "shape": "diamond"},
        {"id": "yes","x": 350, "y": 124, "w": 110, "h": 32,
         "text": "GRANT\n(bypass all checks)", "fill": green, "stroke": "#2d7a3d"},
        {"id": "mt", "x": 165, "y": 72, "w": 140, "h": 38,
         "text": "exact (module, sub, action)\nor (module, null, action)?",
         "fill": diamond, "stroke": "#b86c00", "shape": "diamond", "font_size": 6.8},
        {"id": "sc", "x": 165, "y": 22, "w": 140, "h": 38,
         "text": "scope check\nentity / warehouse / floor\nin allowed lists?", "fill": diamond, "stroke": "#b86c00",
         "shape": "diamond", "font_size": 6.8},
        {"id": "fb", "x": 350, "y": 38, "w": 110, "h": 60,
         "text": "403 forbidden\ndetails: {\n  module, sub_module,\n  action }\nclient → /forbidden",
         "fill": red, "stroke": "#a04444", "font_size": 6.5},
        {"id": "gr", "x": 10,  "y": 22, "w": 110, "h": 38,
         "text": "GRANT → handler runs", "fill": green, "stroke": "#2d7a3d"},
    ]
    arrows = [
        {"from": "rq", "to": "ld", "side_from": "s", "side_to": "n"},
        {"from": "ld", "to": "ad", "side_from": "s", "side_to": "n"},
        {"from": "ad", "to": "yes","side_from": "e", "side_to": "w", "label": "yes"},
        {"from": "ad", "to": "mt", "side_from": "s", "side_to": "n", "label": "no"},
        {"from": "mt", "to": "fb", "side_from": "e", "side_to": "n", "label": "no"},
        {"from": "mt", "to": "sc", "side_from": "s", "side_to": "n", "label": "yes"},
        {"from": "sc", "to": "fb", "side_from": "e", "side_to": "s", "label": "out of scope"},
        {"from": "sc", "to": "gr", "side_from": "w", "side_to": "e", "label": "in scope"},
    ]
    return FlowDiagram(W, H, boxes, arrows, title="Figure 5 — Authorization (RBAC) decision tree")


# ─── Content tables / blocks ─────────────────────────────────────────────


def hard_rules_table():
    rows = [
        ["#", "Rule (frontend MUST follow)"],
        ["1", "Never persist tokens in plain storage. Electron: safeStorage.encryptString; Android: EncryptedSharedPreferences + MasterKey AES256_GCM."],
        ["2", "Send refresh_token ONLY on /auth/refresh and /auth/logout. Access token on every other call."],
        ["3", "Never log tokens. Mask as 'Bearer ****' and strip from crash reports."],
        ["4", "Treat JWT as opaque. May decode exp to schedule proactive refresh — never to make security decisions."],
        ["5", "permissions[] from /me is a UX hint only. Server is authoritative on every call."],
        ["6", "Capture X-Request-ID (or details.request_id) and surface it in every user-facing error toast."],
        ["7", "Phone: send raw user input (4 accepted formats). Server normalizes to E.164."],
        ["8", "/refresh rotates BOTH tokens. Replace both atomically; reusing an old refresh revokes the whole chain."],
        ["9", "After /password/change, other devices are revoked. Show 'Other devices signed out (N)' toast."],
        ["10", "423 account_locked and 429 rate_limit_exceeded are user-recoverable. Show timer; do NOT auto-retry."],
        ["11", "Concurrent 401s → exactly ONE /refresh; queue others on the result (single-flight)."],
        ["12", "token_reuse_detected, invalid_refresh_token, account_disabled/suspended, /refresh 401 → clear + login."],
    ]
    t = Table(rows, colWidths=[15 * mm, 165 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a2b4a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#999")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f8fc")]),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def error_codes_table():
    rows = [
        ["HTTP", "error code", "details", "Frontend behaviour"],
        ["400", "weak_password", "rules: string[]", "Render rule keys inline; highlight new-password field"],
        ["400", "password_mismatch", "—", "Highlight confirm field"],
        ["401", "invalid_credentials", "—", "Generic message; do NOT distinguish unknown-phone vs wrong-password"],
        ["401", "invalid_access_token", "—", "Trigger refresh-and-retry once; on failure → re-login"],
        ["401", "invalid_refresh_token", "—", "Clear tokens, route to login"],
        ["401", "token_expired", "—", "From /refresh → clear + login. From normal call → impossible (interceptor)"],
        ["401", "token_reuse_detected", "—", "Clear tokens, route to login, show 'Security alert: please sign in again.'"],
        ["401", "invalid_old_password", "—", "Highlight Old Password field"],
        ["403", "account_suspended", "—", "'Your account is suspended. Contact your admin.'"],
        ["403", "account_disabled", "—", "'Your account is disabled.'"],
        ["403", "forbidden", "module, sub_module, action", "Redirect to /forbidden; show which permission was missing"],
        ["404", "session_not_found", "—", "On Sessions screen: 'Session no longer exists — refreshing list'"],
        ["404", "user_not_found", "—", "(Admin reset-password) 'User not found'"],
        ["409", "varies", "—", "Show message (e.g. 'Phone number already registered')"],
        ["422", "validation_error", "errors[]", "FastAPI body validation — show first error inline"],
        ["423", "account_locked", "locked_until, failed_login_count", "Disable submit; countdown to locked_until"],
        ["429", "rate_limit_exceeded", "retry_after_seconds, limit, window_seconds", "Disable submit; honour Retry-After header"],
        ["500", "internal_error", "—", "Generic + show request_id from envelope"],
    ]
    t = Table(rows, colWidths=[12 * mm, 38 * mm, 38 * mm, 92 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a2b4a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (1, 1), (1, -1), "Courier"),
        ("FONTNAME", (2, 1), (2, -1), "Courier"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#999")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f8fc")]),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


# ─── Page templates ──────────────────────────────────────────────────────


def _header_footer(canvas: canvas.Canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#1a2b4a"))
    canvas.setFillColor(colors.HexColor("#1a2b4a"))
    canvas.setFont("Helvetica-Bold", 8)
    canvas.drawString(20 * mm, A4[1] - 12 * mm, "Candor Consumption Backend — Auth & Authorization Flow")
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(colors.HexColor("#666"))
    canvas.drawRightString(A4[0] - 20 * mm, A4[1] - 12 * mm, "v1 · 2026-05-18")
    canvas.line(20 * mm, A4[1] - 14 * mm, A4[0] - 20 * mm, A4[1] - 14 * mm)
    # Footer
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(colors.HexColor("#666"))
    canvas.drawCentredString(A4[0] / 2, 10 * mm, f"Page {doc.page}")
    canvas.drawString(20 * mm, 10 * mm, "Source: modules/auth/router.py · modules/auth/schemas.py · Plan/Frontend/1_Authentication_and_Authorization.md")
    canvas.restoreState()


def build():
    doc = BaseDocTemplate(
        str(OUT_PATH),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=15 * mm,
        title="Candor Consumption — Auth & Authorization Flow",
        author="Candor Backend",
    )
    frame = Frame(
        doc.leftMargin, doc.bottomMargin,
        doc.width, doc.height - 6 * mm,
        id="main", showBoundary=0,
    )
    doc.addPageTemplates([PageTemplate(id="main", frames=frame, onPage=_header_footer)])

    story = []

    # ── Cover ──────────────────────────────────────────────────────────
    story.append(Paragraph("Authentication &amp; Authorization", H1))
    story.append(Paragraph("Detailed flow diagrams and frontend payload reference for the Candor Consumption FastAPI backend.", BODY))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "<b>Base URL (prod):</b> https://desktop-backend-vhf0.onrender.com<br/>"
        "<b>Base URL (local):</b> http://localhost:8000<br/>"
        "<b>Auth prefix:</b> /api/v1/auth<br/>"
        "<b>Header on every call after login:</b> Authorization: Bearer &lt;access_token&gt;<br/>"
        "<b>Algorithm:</b> JWT HS256 · Issuer: candor-consumption<br/>"
        "<b>Access TTL:</b> 900 s (15 min) · <b>Refresh TTL:</b> 28800 s (8 h) — read from response, never hardcode.",
        BODY,
    ))
    story.append(Spacer(1, 10))

    # ── Section 1: Architecture ────────────────────────────────────────
    story.append(Paragraph("1. End-to-end architecture", H2))
    story.append(diagram_high_level())
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "Two clients (Electron desktop, native Android) speak to the same FastAPI auth router. "
        "Every response carries an error-envelope-compatible shape, <code>X-Request-ID</code>, "
        "<code>Cache-Control: no-store</code>, and <code>X-Content-Type-Options: nosniff</code>. "
        "The <code>get_current_user</code> dependency validates the bearer token, then the service "
        "layer (jwt, password rules, rate limiter, permissions, phone normalizer) talks to Postgres "
        "via an asyncpg pool.",
        BODY,
    ))
    story.append(PageBreak())

    # ── Section 2: Hard rules ──────────────────────────────────────────
    story.append(Paragraph("2. Hard rules (frontend MUST follow)", H2))
    story.append(hard_rules_table())
    story.append(Spacer(1, 8))

    # ── Section 3: Login flow ─────────────────────────────────────────
    story.append(Paragraph("3. Login flow — POST /api/v1/auth/login", H2))
    story.append(Paragraph(
        "Public endpoint. Rate-limited per (ip, phone). Failed-password UPDATE runs in autocommit "
        "(do <b>not</b> wrap in a transaction — that would roll the counter back and disable lockout).",
        BODY,
    ))
    story.append(Spacer(1, 4))
    story.append(diagram_login_flow())
    story.append(PageBreak())

    # ── Section 4: Refresh + protected call ──────────────────────────
    story.append(Paragraph("4. Protected request + transparent refresh", H2))
    story.append(Paragraph(
        "On every protected route, FastAPI's <code>Depends(get_current_user)</code> verifies the JWT "
        "and loads the <code>AuthUser</code> context. A 401 from a normal call triggers the client's "
        "single-flight refresh: exactly one <code>/auth/refresh</code> runs and queued requests wait "
        "on its result. The original request retries <i>once</i>; another 401 forces re-login. "
        "Refresh-token rotation is mandatory — reusing an old <code>jti</code> revokes the entire chain.",
        BODY,
    ))
    story.append(Spacer(1, 4))
    story.append(diagram_protected_call_and_refresh())
    story.append(PageBreak())

    # ── Section 5: Authorization ──────────────────────────────────────
    story.append(Paragraph("5. Authorization (RBAC) decision tree", H2))
    story.append(Paragraph(
        "Admins (<code>is_admin=TRUE</code> on either the user or any of their roles) bypass all "
        "permission checks. For non-admins the server looks for an exact "
        "<code>(module, sub_module, action)</code> grant or a broader <code>(module, null, action)</code> "
        "grant on any of the user's roles, then applies the scope filter "
        "(<code>allowed_entities</code> / <code>allowed_warehouses</code> / <code>allowed_floors</code>). "
        "Frontend <code>can()</code> is a UX hint only — every API call is independently authorized.",
        BODY,
    ))
    story.append(Spacer(1, 4))
    story.append(diagram_authz())
    story.append(PageBreak())

    # ── Section 6: Logout + password change ──────────────────────────
    story.append(Paragraph("6. Logout, logout-all, change-password, admin reset", H2))
    story.append(diagram_logout_and_password())
    story.append(Spacer(1, 8))

    # ── Section 7: Payload reference ─────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("7. Expected frontend payloads — endpoint by endpoint", H2))
    story.append(Paragraph(
        "Field types reflect <code>modules/auth/schemas.py</code>. <code>min_length=1</code> on every "
        "string field — the frontend should validate non-empty before submit to avoid 422 round-trips.",
        BODY,
    ))

    # 7.1 Login
    story.append(Paragraph("7.1 POST /api/v1/auth/login &nbsp;<i>(no auth header)</i>", H3))
    story.append(Paragraph("<b>Request body</b>", BODY))
    story.append(Preformatted(
        '{\n'
        '  "phone": "9876543210",                          // required, raw user input; server normalizes\n'
        '  "password": "MyPass1234!",                      // required, plain text over HTTPS\n'
        '  "device_info": {                                // OPTIONAL but recommended (free-form, persisted)\n'
        '    "device_id":   "stable-uuid-per-install",     // stable across launches\n'
        '    "device_name": "Kaushal\'s Pixel 8",           // user-readable label for /sessions\n'
        '    "app_version": "1.1.0",\n'
        '    "platform":    "android"                      // "android" | "ios" | "electron" | "web"\n'
        '  }\n'
        '}',
        CODE,
    ))
    story.append(Paragraph("<b>Headers (none required)</b>. Do <i>not</i> send Authorization on login.", SMALL))
    story.append(Paragraph(
        "<b>Validation rules</b>: <code>phone</code> accepts <code>9876543210</code>, <code>09876543210</code>, "
        "<code>+919876543210</code>, <code>919876543210</code>. <code>device_info</code> may carry extra "
        "fields — server is forward-compatible (<code>extra=allow</code>).",
        SMALL,
    ))
    story.append(Spacer(1, 4))

    # 7.2 Refresh
    story.append(Paragraph("7.2 POST /api/v1/auth/refresh &nbsp;<i>(no auth header)</i>", H3))
    story.append(Preformatted(
        '{\n'
        '  "refresh_token": "<current refresh JWT>"        // required\n'
        '}',
        CODE,
    ))
    story.append(Paragraph(
        "Do not send <code>Authorization</code>. On 200, persist <i>both</i> rotated tokens atomically.",
        SMALL,
    ))

    # 7.3 Logout
    story.append(Paragraph("7.3 POST /api/v1/auth/logout &nbsp;<i>(Bearer required)</i>", H3))
    story.append(Preformatted(
        '{\n'
        '  "refresh_token": "<current refresh JWT>"        // required — the session to kill\n'
        '}',
        CODE,
    ))
    story.append(Paragraph(
        "Idempotent: silently 204s even for a stolen / cross-user / already-revoked token. "
        "Always clear local tokens after calling, regardless of HTTP status.",
        SMALL,
    ))

    # 7.4 Logout-all
    story.append(Paragraph("7.4 POST /api/v1/auth/logout-all &nbsp;<i>(Bearer required)</i>", H3))
    story.append(Paragraph("Empty body. Response: <code>{ \"revoked_count\": N }</code>.", SMALL))

    # 7.5 /me
    story.append(Paragraph("7.5 GET /api/v1/auth/me &nbsp;<i>(Bearer required)</i>", H3))
    story.append(Paragraph("No request body. No query params. Headers: <code>Authorization: Bearer …</code>.", SMALL))

    # 7.6 Password change
    story.append(Paragraph("7.6 POST /api/v1/auth/password/change &nbsp;<i>(Bearer required)</i>", H3))
    story.append(Preformatted(
        '{\n'
        '  "old_password":     "OldPass1234",              // required\n'
        '  "new_password":     "NewStrongPass5678",        // required, must satisfy §6.1 rules\n'
        '  "confirm_password": "NewStrongPass5678"         // required, must == new_password\n'
        '}',
        CODE,
    ))
    story.append(Paragraph(
        "Server validates: 12–128 chars, ≥1 letter AND ≥1 digit, must not equal/contain the user's "
        "phone (≥7-digit suffix), not in common-passwords blocklist. Pre-validate the first three "
        "rules client-side for snappy UX; let the server return 400 <code>weak_password</code> with "
        "<code>details.rules</code> for the blocklist check.",
        SMALL,
    ))

    # 7.7 Sessions
    story.append(Paragraph("7.7 GET /api/v1/auth/sessions &nbsp;<i>(Bearer required)</i>", H3))
    story.append(Paragraph("No body. Returns the user's live refresh sessions with <code>is_current</code> on the active one.", SMALL))
    story.append(Paragraph("7.8 DELETE /api/v1/auth/sessions/{token_id} &nbsp;<i>(Bearer required)</i>", H3))
    story.append(Paragraph(
        "Path param: <code>token_id</code> from /sessions. No body. 204 on success, 404 "
        "<code>session_not_found</code> if it doesn't exist <i>or</i> belongs to another user (no leak). "
        "Revoking your own current session signs you out.",
        SMALL,
    ))

    story.append(PageBreak())

    # ── Section 8: Admin payloads ─────────────────────────────────────
    story.append(Paragraph("8. Admin-only endpoint payloads &nbsp;<i>(Bearer + is_admin)</i>", H2))

    story.append(Paragraph("8.1 POST /api/v1/auth/users — create user", H3))
    story.append(Preformatted(
        '{\n'
        '  "phone":              "9876543211",             // required, accepts the same 4 formats\n'
        '  "password":           "TempPass1234",           // required, same policy as /password/change\n'
        '  "full_name":          "Ramesh Kumar",           // required\n'
        '  "role_id":            4,                        // required, must exist in auth_role\n'
        '  "email":              "ramesh@candorfoods.in",  // optional, nullable\n'
        '  "entity":             "cfpl",                   // optional: "cfpl" | "cdpl" | null\n'
        '  "allowed_warehouses": ["W202"]                  // optional, array of warehouse codes\n'
        '}',
        CODE,
    ))
    story.append(Paragraph(
        "409 on duplicate phone (constraint <code>phone</code>). 400 <code>weak_password</code> with "
        "<code>details.rules</code> if the temp password fails policy.",
        SMALL,
    ))

    story.append(Paragraph("8.2 PUT /api/v1/auth/users/{user_id} — partial edit", H3))
    story.append(Preformatted(
        '{                                                  // every key OPTIONAL — only send what changes\n'
        '  "full_name":          "Ramesh K.",\n'
        '  "email":              "ramesh.k@candorfoods.in",\n'
        '  "role_id":            2,\n'
        '  "entity":             "cfpl",\n'
        '  "is_active":          false,                    // soft-delete via DELETE is preferred\n'
        '  "allowed_warehouses": ["W202","W301"]\n'
        '}',
        CODE,
    ))
    story.append(Paragraph(
        "Server allowlists fields: <code>full_name, email, role_id, entity, is_active, allowed_warehouses</code>. "
        "Anything else is silently dropped — sending <code>password_encrypted</code>, <code>is_admin</code>, etc. "
        "is a no-op.",
        SMALL,
    ))

    story.append(Paragraph("8.3 DELETE /api/v1/auth/users/{user_id} — deactivate", H3))
    story.append(Paragraph(
        "No body. Sets <code>is_active=FALSE</code> and revokes all the target's live refresh tokens "
        "with <code>revoke_reason='admin_revoked'</code>.",
        SMALL,
    ))

    story.append(Paragraph("8.4 POST /api/v1/auth/users/{user_id}/reset-password", H3))
    story.append(Preformatted(
        '{\n'
        '  "new_password": "TempStrongPass1234"            // required, 1–256 chars, same policy as /password/change\n'
        '}',
        CODE,
    ))
    story.append(Paragraph(
        "Forces <code>must_change_password=TRUE</code> on the target, clears any lockout, and revokes "
        "all the target's live refresh tokens. Response includes <code>{ user_id, message, revoked_count, "
        "temp_password_set: true }</code>.",
        SMALL,
    ))

    story.append(Paragraph("8.5 POST /api/v1/auth/roles — create role", H3))
    story.append(Preformatted(
        '{\n'
        '  "role_name":   "qc_lead",                       // required, unique\n'
        '  "description": "QC team lead",                  // optional, default ""\n'
        '  "is_admin":    false                            // optional, default false\n'
        '}',
        CODE,
    ))

    story.append(Paragraph("8.6 PUT /api/v1/auth/roles/{role_id}/permissions — replace all", H3))
    story.append(Preformatted(
        '{\n'
        '  "permission_ids":     [1, 2, 5, 10, 24, 25],    // required; replaces every existing mapping\n'
        '  "allowed_entities":   ["cfpl"],                 // optional, null = all entities\n'
        '  "allowed_warehouses": ["W202"],                 // optional, null = all warehouses\n'
        '  "allowed_floors":     ["1st Floor"]             // optional, null = all floors\n'
        '}',
        CODE,
    ))

    story.append(Paragraph("8.7 POST /api/v1/auth/permissions/create", H3))
    story.append(Preformatted(
        '{\n'
        '  "module":         "production",                 // required\n'
        '  "sub_module":     "plans",                      // optional, nullable\n'
        '  "sub_sub_module": "approve",                    // optional, nullable\n'
        '  "action":         "create",                     // required\n'
        '  "description":    "Approve a production plan"   // optional, default ""\n'
        '}',
        CODE,
    ))

    story.append(Paragraph("8.8 PUT /api/v1/auth/permissions/{permission_id}", H3))
    story.append(Paragraph(
        "Partial edit; allowlist = <code>module, sub_module, sub_sub_module, action, description</code>. "
        "Any other key is silently dropped.",
        SMALL,
    ))

    story.append(Paragraph("8.9 POST /api/v1/auth/modules — bulk create permissions", H3))
    story.append(Preformatted(
        '{\n'
        '  "module":      "quality",                       // required\n'
        '  "sub_modules": ["inspections", "calibration"]   // optional; null = create module-level only\n'
        '}\n'
        '// Auto-creates view / create / edit / delete for each (module, sub_module).\n'
        '// Returns: { module, sub_modules, permissions_created: N }',
        CODE,
    ))

    story.append(PageBreak())

    # ── Section 9: Error envelope + codes ─────────────────────────────
    story.append(Paragraph("9. Error envelope &amp; codes", H2))
    story.append(Paragraph(
        "Every non-2xx response has this shape. The <code>X-Request-ID</code> response header always "
        "carries the same UUID as <code>request_id</code>. Surface the request_id in every user-facing "
        "error toast — support uses it to grep backend logs.",
        BODY,
    ))
    story.append(Preformatted(
        '{\n'
        '  "error":      "<machine_code>",                 // stable; switch on this in code\n'
        '  "message":    "<human_readable>",               // safe to show to users\n'
        '  "request_id": "<uuid>",                         // = response header X-Request-ID\n'
        '  "timestamp":  "2026-05-07T10:23:45.123Z",       // ISO-8601 UTC\n'
        '  "details":    { }                               // shape varies by error (see table)\n'
        '}',
        CODE,
    ))
    story.append(Spacer(1, 4))
    story.append(error_codes_table())

    story.append(PageBreak())

    # ── Section 10: Response payloads (quick reference) ─────────────
    story.append(Paragraph("10. Response payload quick reference", H2))

    story.append(Paragraph("10.1 LoginResponse / RefreshResponse", H3))
    story.append(Preformatted(
        '{\n'
        '  "access_token":        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",\n'
        '  "refresh_token":       "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",\n'
        '  "token_type":          "Bearer",\n'
        '  "expires_in":          900,                     // access TTL, seconds\n'
        '  "refresh_expires_in":  28800,                   // refresh TTL, seconds\n'
        '  "must_change_password": false,                  // login response only — gates navigation\n'
        '  "user": {                                       // login response only\n'
        '    "user_id":   "1",\n'
        '    "phone":     "+919876543210",                 // normalized E.164\n'
        '    "full_name": "Kaushal Patel",\n'
        '    "email":     "kaushal@candorfoods.in",\n'
        '    "is_admin":  true,\n'
        '    "roles": [\n'
        '      { "role_id": "1", "code": "admin",\n'
        '        "label": "Full unrestricted access", "is_admin": true }\n'
        '    ]\n'
        '  }\n'
        '}',
        CODE,
    ))

    story.append(Paragraph("10.2 MeResponse", H3))
    story.append(Preformatted(
        '{\n'
        '  "user_id":              "1",\n'
        '  "phone":                "+919876543210",\n'
        '  "full_name":            "Kaushal Patel",\n'
        '  "email":                "kaushal@candorfoods.in",\n'
        '  "status":               "active",               // "active" | "suspended" | "disabled"\n'
        '  "must_change_password": false,\n'
        '  "is_admin":             true,\n'
        '  "roles":                [ { "role_id": "1", "code": "admin", ... } ],\n'
        '  "permissions": [                                // UX-hint only; server is authoritative\n'
        '    { "module": "production", "sub_module": "plans",     "action": "view"   },\n'
        '    { "module": "production", "sub_module": "job_cards", "action": "view"   }\n'
        '  ],\n'
        '  "entities":             ["cfpl"],               // user-level scope defaults\n'
        '  "warehouses":           ["W202"],\n'
        '  "floors":               [],\n'
        '  "last_login_at":        "2026-05-07T10:00:00Z",\n'
        '  "password_changed_at":  "2026-04-30T08:30:00Z"\n'
        '}',
        CODE,
    ))

    story.append(Paragraph("10.3 SessionsResponse (one row)", H3))
    story.append(Preformatted(
        '{\n'
        '  "token_id":    "f81d4fae-7dec-11d0-a765-00a0c91e6bf6",\n'
        '  "token_type":  "refresh",\n'
        '  "issued_at":   "2026-05-07T08:00:00Z",\n'
        '  "expires_at":  "2026-05-07T16:00:00Z",\n'
        '  "ip":          "203.0.113.42",\n'
        '  "user_agent":  "Mozilla/5.0 ...",\n'
        '  "device_info": {                                // whatever the client sent on /login\n'
        '    "device_id":   "stable-uuid",\n'
        '    "device_name": "Kaushal\'s Pixel 8",\n'
        '    "platform":    "android"\n'
        '  },\n'
        '  "is_current":  true                             // matches current refresh jti\n'
        '}',
        CODE,
    ))

    story.append(Spacer(1, 8))
    story.append(Paragraph("11. Source pointers", H2))
    src_rows = [
        ["Concern", "File"],
        ["Endpoints + wire shapes", "modules/auth/router.py · modules/auth/schemas.py"],
        ["Login / refresh / rotation", "modules/auth/services/auth_service.py"],
        ["JWT issuance + verification", "modules/auth/services/jwt_service.py"],
        ["Permission engine", "modules/auth/services/permission_service.py"],
        ["Password rules", "modules/auth/services/password_rules.py"],
        ["Phone normalization", "modules/auth/services/phone.py"],
        ["Rate limiter", "modules/auth/services/rate_limiter.py"],
        ["RBAC middleware", "modules/auth/middleware.py"],
        ["Error envelope middleware", "core/middleware/request_context.py"],
        ["DB schema", "db/auth_schema.sql"],
        ["Token TTL / lockout settings", "app/config.py (Settings.ACCESS_TOKEN_TTL_SECONDS, …)"],
    ]
    t = Table(src_rows, colWidths=[55 * mm, 125 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a2b4a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (1, 1), (1, -1), "Courier"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#999")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f8fc")]),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(t)

    doc.build(story)


if __name__ == "__main__":
    build()
    print(f"Wrote {OUT_PATH}")
