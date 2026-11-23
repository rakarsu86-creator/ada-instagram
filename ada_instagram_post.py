#!/usr/bin/env python3
"""Ada Gümrük – günlük Instagram gönderisi.

Kullanım:
  python ada_instagram_post.py draft    # taslağı üretir, Telegram'a gönderir
  python ada_instagram_post.py publish  # /iptal yazılmadıysa Instagram'da yayınlar
"""
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont

# ---------------- AYARLAR (gerekirse düzenleyin) ----------------
MODEL = "claude-sonnet-5-5"
GRAPH = "https://graph.facebook.com/v26.0"
TZ = ZoneInfo("Europe/Istanbul")
ROOT = Path(__file__).parent

KAYNAKLAR = [
    ("Resmî Gazete (bugün)", "https://www.resmigazete.gov.tr/"),
    ("Ticaret Bakanlığı duyuruları", "https://ticaret.gov.tr/duyurular"),  # URL'yi tarayıcıdan kontrol edin
]

# Görsel renkleri (marka renklerinize göre değiştirin)
NAVY = (11, 37, 69)
GOLD = (201, 162, 39)
WHITE = (255, 255, 255)
SOFT = (216, 222, 233)
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
# -----------------------------------------------------------------

SISTEM = """Sen Ada Gümrük Müşavirliği ve Danışmanlık için Instagram içeriği hazırlayan bir gümrük mevzuatı editörüsün. Türkçe, sade, kurumsal ve güvenilir yaz.

KESİN KURALLAR:
- "haber" modunda yalnızca kaynak metinde gerçekten yazan bilgiyi kullan. Sayı, tarih, kanun/tebliğ/karar numarası veya oran uydurma; emin değilsen o ayrıntıyı yazma.
- "rehber" modunda yalnızca yerleşik, genel kabul görmüş bilgiyi anlat. Güncel oran, tutar, tarih veya sık değişen ayrıntı verme.
- Hukuki veya mali tavsiye verme. Bilgilendirme tonunda kal.
- Yanıtın SADECE geçerli bir JSON nesnesi olsun, başka metin ekleme.

JSON şeması:
{
  "mod": "haber" veya "rehber",
  "baslik": "en fazla 60 karakter",
  "maddeler": ["en fazla 4 madde, her biri en fazla 90 karakter"],
  "caption": "Instagram açıklaması: 1 giriş cümlesi, 3-5 kısa paragraf veya madde, son satırda 5-8 ilgili hashtag",
  "kaynak": "haber modunda kaynak adı ve varsa URL; rehber modunda boş bırak",
  "kontrol_notu": "Yayından önce insanın doğrulaması gereken iddiaları 1-3 cümleyle yaz"
}"""


def env(key):
    v = os.environ.get(key)
    if not v:
        raise RuntimeError(f"{key} tanımlı değil (GitHub Secrets'ı kontrol edin)")
    return v


# ---------------- Telegram ----------------
def tg(method, **kw):
    token = env("TELEGRAM_BOT_TOKEN")
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", timeout=60, **kw)
    r.raise_for_status()
    return r.json()


def tg_msg(text):
    tg("sendMessage", data={"chat_id": env("TELEGRAM_CHAT_ID"), "text": text[:4000]})


def tg_photo(path, caption):
    with open(path, "rb") as f:
        tg("sendPhoto", data={"chat_id": env("TELEGRAM_CHAT_ID"), "caption": caption[:1000]}, files={"photo": f})


def iptal_istendi(since_ts):
    """Taslak gönderildikten sonra sohbetten /iptal yazıldı mı?"""
    chat = str(env("TELEGRAM_CHAT_ID"))
    res = tg("getUpdates", params={"timeout": 0}).get("result", [])
    for u in res:
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) == chat and m.get("date", 0) >= since_ts:
            if (m.get("text") or "").strip().lower().startswith("/iptal"):
                return True
    return False


# ---------------- Kaynak + Claude ----------------
def temizle(html):
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    html = html.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", html).strip()


def kaynak_metni():
    out = ""
    for ad, url in KAYNAKLAR:
        try:
            r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            r.encoding = r.encoding or "utf-8"
            out += f"\n### {ad} ({url})\n" + temizle(r.text)[:6000]
        except Exception:
            out += f"\n### {ad}: alınamadı\n"
    return out


def claude_icerik(konu, bugun, metin):
    kullanici = f"""Bugünün tarihi: {bugun}.

Aşağıdaki kaynak metinlerde bugün yayımlanmış, gümrük, dış ticaret, ithalat/ihracat veya bunlarla ilgili vergilendirmeye doğrudan ilişkin YENİ bir düzenleme ya da duyuru varsa mod="haber" seç ve yalnızca onu anlat. Yoksa mod="rehber" seç ve şu rehber konusunu anlat: "{konu}".

KAYNAK METİNLER:
{metin}"""
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": env("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": 1500, "system": SISTEM,
              "messages": [{"role": "user", "content": kullanici}]},
        timeout=120,
    )
    r.raise_for_status()
    metin_out = "".join(b.get("text", "") for b in r.json()["content"])
    m = re.search(r"\{.*\}", metin_out, re.S)
    d = json.loads(m.group(0))
    if not d.get("baslik") or not d.get("maddeler"):
        raise RuntimeError("Claude yanıtında başlık/maddeler eksik")
    return d


# ---------------- Görsel ----------------
def font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def sar(draw, text, fnt, max_w):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=fnt) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def gorsel_uret(d, yol):
    W = H = 1080
    PAD = 80
    max_w = W - 2 * PAD
    img = Image.new("RGB", (W, H), NAVY)
    dr = ImageDraw.Draw(img)

    # Etiket
    etiket = "MEVZUAT GÜNDEMİ" if d["mod"] == "haber" else "GÜMRÜK REHBERİ"
    f_et = font(FONT_BOLD, 28)
    tw = dr.textlength(etiket, font=f_et)
    dr.rounded_rectangle([PAD, 80, PAD + tw + 52, 80 + 58], radius=8, fill=GOLD)
    dr.text((PAD + 26, 80 + 12), etiket, font=f_et, fill=NAVY)

    # Başlık
    y = 190
    f_bas = font(FONT_BOLD, 64)
    for ln in sar(dr, d["baslik"], f_bas, max_w)[:3]:
        dr.text((PAD, y), ln, font=f_bas, fill=WHITE)
        y += 80
    y += 30

    # Maddeler: sığana kadar yazıyı küçült
    maddeler = [str(m) for m in d["maddeler"][:4]]
    alt_sinir = 930
    for size in (38, 34, 30, 26):
        f_m = font(FONT_REG, size)
        satir_h = int(size * 1.35)
        bloklar = [sar(dr, m, f_m, max_w - 44) for m in maddeler]
        toplam = sum(len(b) * satir_h + 20 for b in bloklar)
        if y + toplam <= alt_sinir:
            break
    for b in bloklar:
        dr.ellipse([PAD, y + satir_h // 2 - 9, PAD + 18, y + satir_h // 2 + 9], fill=GOLD)
        for ln in b:
            dr.text((PAD + 44, y), ln, font=f_m, fill=WHITE)
            y += satir_h
        y += 20

    # Alt bilgi
    dr.line([PAD, 985, W - PAD, 985], fill=GOLD, width=2)
    f_alt = font(FONT_REG, 28)
    dr.text((PAD, 1005), "Ada Gümrük Müşavirliği", font=f_alt, fill=SOFT)
    sag = "@adagumrukcom"
    dr.text((W - PAD - dr.textlength(sag, font=f_alt), 1005), sag, font=f_alt, fill=SOFT)

    img.save(yol, "JPEG", quality=92)


# ---------------- Instagram ----------------
def ig_yayinla(image_url, caption):
    uid, tok = env("IG_USER_ID"), env("IG_ACCESS_TOKEN")
    r = requests.post(f"{GRAPH}/{uid}/media", timeout=60,
                      data={"image_url": image_url, "caption": caption[:2200], "access_token": tok})
    if not r.ok:
        raise RuntimeError(f"Instagram container hatası: {r.text}")
    cid = r.json()["id"]
    for _ in range(12):
        s = requests.get(f"{GRAPH}/{cid}", timeout=30,
                         params={"fields": "status_code", "access_token": tok}).json()
        if s.get("status_code") == "FINISHED":
            break
        if s.get("status_code") == "ERROR":
            raise RuntimeError(f"Instagram container ERROR: {s}")
        time.sleep(5)
    r = requests.post(f"{GRAPH}/{uid}/media_publish", timeout=60,
                      data={"creation_id": cid, "access_token": tok})
    if not r.ok:
        raise RuntimeError(f"Instagram yayın hatası: {r.text}")
    return r.json()["id"]


# ---------------- Akışlar ----------------
def bugun_klasor():
    return ROOT / "posts" / datetime.now(TZ).strftime("%Y-%m-%d")


def draft():
    konular = json.loads((ROOT / "konular.json").read_text(encoding="utf-8"))["rehber_konulari"]
    state_yol = ROOT / "state.json"
    state = json.loads(state_yol.read_text()) if state_yol.exists() else {"sira": 0}
    konu = konular[state["sira"] % len(konular)]
    state["sira"] += 1

    bugun = datetime.now(TZ).strftime("%d.%m.%Y")
    d = claude_icerik(konu, bugun, kaynak_metni())

    klasor = bugun_klasor()
    klasor.mkdir(parents=True, exist_ok=True)
    gorsel_uret(d, klasor / "post.jpg")

    caption = d.get("caption", "").strip()
    if d.get("kaynak"):
        caption += f"\n\nKaynak: {d['kaynak']}"
    caption += "\n\nBu paylaşım bilgilendirme amaçlıdır; somut durumunuz için müşavirinize danışın."

    (klasor / "post.json").write_text(json.dumps({
        "mod": d["mod"], "baslik": d["baslik"], "caption": caption,
        "kontrol_notu": d.get("kontrol_notu", ""), "draft_ts": int(time.time()), "durum": "taslak",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    state_yol.write_text(json.dumps(state))

    tg_photo(klasor / "post.jpg", f"📝 Bugünün taslağı ({d['mod']})")
    tg_msg(f"{caption}\n\n⚠️ Kontrol: {d.get('kontrol_notu', '-')}\n\n"
           "Öğlen 12:00'de otomatik yayınlanacak. Yayınlanmasını istemiyorsanız o saate kadar bu sohbete /iptal yazın.")


def publish():
    klasor = bugun_klasor()
    pj = klasor / "post.json"
    if not pj.exists():
        tg_msg("ℹ️ Bugün için taslak bulunamadı, yayın yapılmadı.")
        return
    p = json.loads(pj.read_text(encoding="utf-8"))
    if p["durum"] != "taslak":
        return

    if iptal_istendi(p["draft_ts"]):
        p["durum"] = "iptal"
        pj.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
        (klasor / "post.jpg").unlink(missing_ok=True)
        tg_msg("🛑 Bugünün gönderisi iptal edildi, yayınlanmadı.")
        return

    repo, dal = env("GITHUB_REPOSITORY"), os.environ.get("GITHUB_REF_NAME", "main")
    tarih = klasor.name
    url = f"https://raw.githubusercontent.com/{repo}/{dal}/posts/{tarih}/post.jpg"
    media_id = ig_yayinla(url, p["caption"])
    p["durum"] = "yayinlandi"
    pj.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    tg_msg(f"✅ Instagram'da yayınlandı. (ID: {media_id})")


if __name__ == "__main__":
    komut = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        {"draft": draft, "publish": publish}[komut]()
    except KeyError:
        print("Kullanım: python ada_instagram_post.py [draft|publish]")
        sys.exit(2)
    except Exception as e:
        try:
            tg_msg(f"❌ Hata ({komut}): {e}")
        except Exception:
            pass
        raise
