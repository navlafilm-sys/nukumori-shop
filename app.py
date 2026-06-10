"""
누쿠모리샵 상품 자동 등록 도구
타오바오/1688/아마존JP URL → 한국어 번역 → 스마트스토어 등록
"""

import streamlit as st
import asyncio
import re
import json
import time
import base64
import bcrypt
import requests
from deep_translator import GoogleTranslator
from urllib.parse import urlparse
from playwright.async_api import async_playwright

# ─────────────────────────────────────────────
# CONFIG — 누쿠모리샵 키 (config_ss.json 기준)
# ─────────────────────────────────────────────
NAVER_CLIENT_ID = st.secrets.get("NAVER_CLIENT_ID", "77mQwVJKYYbW7SkMgYoK5B")
NAVER_CLIENT_SECRET = st.secrets.get("NAVER_CLIENT_SECRET", "$2a$04$VOcexymHHlxRXyDGS6ons.")
MARGIN_MULTIPLIER = 2.0         # 원가 × 2 = 판매가 (소수점 가능, 예: 2.5)
DEFAULT_STOCK     = 99          # 기본 재고 수량

# CNY → KRW, JPY → KRW 환율 (직접 수정 또는 실시간 API 연동 가능)
CNY_TO_KRW = 190
JPY_TO_KRW = 9.5

# ─────────────────────────────────────────────
# 사이트 감지
# ─────────────────────────────────────────────
def detect_site(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "taobao.com" in host:   return "taobao"
    if "1688.com" in host:     return "1688"
    if "amazon.co.jp" in host: return "amazon_jp"
    return "unknown"

# ─────────────────────────────────────────────
# Playwright 스크래퍼
# ─────────────────────────────────────────────
async def scrape_product(url: str) -> dict:
    site = detect_site(url)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            locale="ko-KR",
        )
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=40000)
        await asyncio.sleep(2)

        if site == "taobao":
            data = await _scrape_taobao(page)
        elif site == "1688":
            data = await _scrape_1688(page)
        elif site == "amazon_jp":
            data = await _scrape_amazon_jp(page)
        else:
            data = await _scrape_generic(page)

        await browser.close()
        data["source_url"] = url
        data["site"] = site
        return data


async def _scrape_taobao(page) -> dict:
    title = await page.title()
    # 상품명
    name = await page.evaluate("""
        () => {
            const el = document.querySelector('.tb-main-title, .mainTitle, h1');
            return el ? el.innerText.trim() : '';
        }
    """) or title

    # 가격 (CNY)
    price_text = await page.evaluate("""
        () => {
            const el = document.querySelector('.tb-price .tb-rmb-num, .Price--minPrice--1KoGYKz, [class*="price"]');
            return el ? el.innerText.trim() : '';
        }
    """) or "0"

    # 이미지 목록
    images = await page.evaluate("""
        () => {
            const imgs = document.querySelectorAll('.tb-gallery-item img, .PicGallery--imgWrap img, [class*="thumbnail"] img');
            return [...imgs].map(i => i.src || i.dataset.src).filter(Boolean).slice(0, 10);
        }
    """)

    # 상세 이미지
    detail_html = await page.evaluate("""
        () => {
            const el = document.querySelector('#description, .tb-desc, [class*="detail"]');
            return el ? el.innerHTML : '';
        }
    """) or ""

    price_num = float(re.sub(r"[^\d.]", "", price_text) or 0)
    price_krw = int(price_num * CNY_TO_KRW)

    return {"name": name, "price_original": price_num, "currency": "CNY",
            "price_krw": price_krw, "images": images, "detail_html": detail_html}


async def _scrape_1688(page) -> dict:
    name = await page.evaluate("""
        () => {
            const el = document.querySelector('.product-name, .title-text, h1');
            return el ? el.innerText.trim() : document.title;
        }
    """)

    price_text = await page.evaluate("""
        () => {
            const el = document.querySelector('.price-common-price .number, .price-num, [class*="price"] .value');
            return el ? el.innerText.trim() : '0';
        }
    """) or "0"

    images = await page.evaluate("""
        () => {
            const imgs = document.querySelectorAll('.img-gallery img, .detail-gallery-turn-img img');
            return [...imgs].map(i => i.src || i.dataset.lazysrc).filter(Boolean).slice(0, 10);
        }
    """)

    detail_html = await page.evaluate("""
        () => {
            const el = document.querySelector('.detail-desc, #mod-detail-desc');
            return el ? el.innerHTML : '';
        }
    """) or ""

    price_num = float(re.sub(r"[^\d.]", "", price_text) or 0)
    price_krw = int(price_num * CNY_TO_KRW)

    return {"name": name, "price_original": price_num, "currency": "CNY",
            "price_krw": price_krw, "images": images, "detail_html": detail_html}


async def _scrape_amazon_jp(page) -> dict:
    name = await page.evaluate("() => document.querySelector('#productTitle')?.innerText.trim() || document.title")

    price_text = await page.evaluate("""
        () => {
            const sel = [
                '#corePriceDisplay_desktop_feature_div .a-price-whole',
                '.apexPriceToPay .a-price-whole',
                '#priceblock_ourprice',
                '#priceblock_dealprice',
            ];
            for (const s of sel) {
                const el = document.querySelector(s);
                if (el) {
                    // innerText 대신 firstChild 텍스트만 (중복 방지)
                    const txt = (el.childNodes[0]?.textContent || el.innerText || "").trim().replace(/,/g,"");
                    if (txt && !isNaN(txt)) return txt;
                }
            }
            return '0';
        }
    """) or "0"

    images = await page.evaluate("""
        () => {
            try {
                const data = window.__MAIN_IMAGE_DATA__ || {};
                const imgs = Object.values(data).flatMap(v => v.hiRes || v.large || []);
                if (imgs.length) return imgs.slice(0, 10);
            } catch(e) {}
            const imgs = document.querySelectorAll('#altImages img, #imageBlock img');
            return [...imgs].map(i => (i.src||'').replace(/\._[A-Z0-9_,]+_\./, '.')).filter(s => s.startsWith('http')).slice(0, 10);
        }
    """)

    detail_html = await page.evaluate("""
        () => {
            const el = document.querySelector('#productDescription, #aplus_feature_div');
            return el ? el.innerHTML : '';
        }
    """) or ""

    # 통화 기호 포함 전체 가격 텍스트 추출
    price_full = await page.evaluate("""
        () => {
            const sel = [
                '#corePriceDisplay_desktop_feature_div .a-price',
                '.apexPriceToPay', '.a-price',
                '#priceblock_ourprice', '#priceblock_dealprice'
            ];
            for (const s of sel) {
                const el = document.querySelector(s);
                if (el && el.innerText.trim()) return el.innerText.trim();
            }
            return '';
        }
    """) or price_text

    # 통화 판별 (기호 우선, 없으면 숫자 크기로)
    raw = price_full.strip()
    num = float(re.sub(r"[^\d.,]", "", raw).replace(",", "") or 0)
    if "₩" in raw or "KRW" in raw:
        currency, price_krw = "KRW", int(num)
    elif "¥" in raw or "￥" in raw or "JPY" in raw:
        currency, price_krw = "JPY", int(num * JPY_TO_KRW)
    elif "$" in raw or "USD" in raw:
        currency, price_krw = "USD", int(num * 1350)
    elif num >= 10000:  # 기호 없고 만 이상 → KRW로 간주
        currency, price_krw = "KRW", int(num)
    else:               # 기호 없고 작으면 → JPY로 간주
        currency, price_krw = "JPY", int(num * JPY_TO_KRW)

    price_num = num
    return {"name": name, "price_original": price_num, "currency": currency,
            "price_krw": price_krw, "images": images, "detail_html": detail_html}


async def _scrape_generic(page) -> dict:
    name = await page.evaluate("() => document.querySelector('h1')?.innerText.trim() || document.title")
    images = await page.evaluate("""
        () => [...document.querySelectorAll('img')].map(i=>i.src).filter(s=>s.startsWith('http') && !s.includes('logo') && !s.includes('icon')).slice(0,10)
    """)
    return {"name": name, "price_original": 0, "currency": "?",
            "price_krw": 0, "images": images, "detail_html": ""}


# ─────────────────────────────────────────────
# Google 번역 (무료, deep-translator)
# ─────────────────────────────────────────────
def translate_to_korean(product: dict) -> dict:
    site = product.get("site", "")
    src_lang = "auto"

    try:
        translator = GoogleTranslator(source=src_lang, target="ko")
        name_kr = translator.translate(product["name"][:200])  # 너무 길면 자르기
        if name_kr and len(name_kr) > 50:
            name_kr = name_kr[:50]
    except Exception:
        try:
            # auto detect로 재시도
            translator = GoogleTranslator(source="auto", target="ko")
            name_kr = translator.translate(product["name"][:200])
            if name_kr and len(name_kr) > 50:
                name_kr = name_kr[:50]
        except Exception:
            name_kr = product["name"]

    # 어린이 관련 단어 제거
    CHILD_WORDS = [
        "소녀", "소년", "아이", "어린이", "아동", "유아", "키즈", "걸즈", "보이즈",
        "girls", "boys", "kids", "children", "child", "junior", "youth", "baby",
        "ガールズ", "ボーイズ", "キッズ", "子供", "女の子", "男の子",
        "女童", "男童", "儿童", "童", "小朋友",
    ]
    for w in CHILD_WORDS:
        name_kr = re.sub(rf"(?i)\b{re.escape(w)}\b", "", name_kr or "").strip()
    name_kr = re.sub(r"\s{2,}", " ", name_kr).strip(" ,&·/")

    # 태그 생성: 번역명 + 원본명 단어 조합, 의미있는 단어만
    child_lower = [c.lower() for c in CHILD_WORDS]
    ko_words = re.findall(r"[가-힣]{2,}", name_kr or "")
    en_words = re.findall(r"[A-Za-z]{3,}", (product.get("name","") + " " + (name_kr or "")))
    en_words = [w for w in en_words if w.lower() not in child_lower + ["the","and","for","with","from","that","this","ref","encoding","utf"]]
    # 영어 단어 한국어 번역 시도
    ko_from_en = []
    if en_words:
        try:
            tr2 = GoogleTranslator(source="auto", target="ko")
            translated_en = tr2.translate(" ".join(en_words[:10]))
            ko_from_en = re.findall(r"[가-힣]{2,}", translated_en or "")
        except Exception:
            pass
    all_tags = list(dict.fromkeys(ko_words + ko_from_en))
    all_tags = [t for t in all_tags if t not in CHILD_WORDS]
    tags = all_tags[:10]  # 최대 10개

    product["name_kr"] = name_kr or product["name"]
    product["summary"] = name_kr[:40] if name_kr else ""
    product["tags"] = tags
    product["translated"] = True
    return product


# ─────────────────────────────────────────────
# 가격 계산
# ─────────────────────────────────────────────
def calculate_sale_price(price_krw: int, multiplier: float = MARGIN_MULTIPLIER) -> int:
    """원가 × 마진율, 100원 단위 올림"""
    raw = price_krw * multiplier
    return int(((raw // 100) + 1) * 100)


# ─────────────────────────────────────────────
# 네이버 커머스 API
# ─────────────────────────────────────────────
def _get_naver_token() -> str:
    """누쿠모리샵: bcrypt 방식 인증 (nukumori_fetch.py 동일 로직)"""
    ts = int(time.time() * 1000)
    hashed = bcrypt.hashpw(f"{NAVER_CLIENT_ID}_{ts}".encode(), NAVER_CLIENT_SECRET.encode())
    sign = base64.b64encode(hashed).decode()

    resp = requests.post(
        "https://api.commerce.naver.com/external/v1/oauth2/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": NAVER_CLIENT_ID,
            "timestamp": ts,
            "client_secret_sign": sign,
            "type": "SELF",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def register_product_naver(product: dict, category_id: str = "50000803") -> dict:
    """
    스마트스토어에 상품 등록.
    category_id: 기본값 50000803 (잡화 > 기타잡화)
                 본인 카테고리에 맞게 수정 필요
    """
    token = _get_naver_token()

    sale_price = calculate_sale_price(product["price_krw"])
    images = product.get("images", [])
    rep_image = {"url": images[0]} if images else {"url": ""}
    opt_images = [{"url": u} for u in images[1:10]]

    tomorrow = time.strftime("%Y-%m-%dT00:00:00.000+09:00",
                              time.localtime(time.time() + 86400))
    far_future = "2099-12-31T00:00:00.000+09:00"

    body = {
        "originProduct": {
            "statusType": "SALE",
            "saleType": "NEW",
            "leafCategoryId": category_id,
            "name": product["name_kr"],
            "images": {
                "representativeImage": rep_image,
                "optionalImages": opt_images,
            },
            "detailContent": product.get("detail_html", ""),
            "saleStartDate": tomorrow,
            "saleEndDate": far_future,
            "salePrice": sale_price,
            "stockQuantity": DEFAULT_STOCK,
            "deliveryInfo": {
                "deliveryType": "DELIVERY",
                "deliveryAttributeType": "NORMAL",
                "deliveryFee": {
                    "deliveryFeeType": "FREE",
                    "baseFee": 0,
                },
                "claimDeliveryInfo": {
                    "returnDeliveryFee": 3000,
                    "exchangeDeliveryFee": 6000,
                },
            },
            "detailAttribute": {
                "naverShoppingSearchInfo": {
                    "modelInfo": {"name": product["name_kr"]},
                },
                "afterServiceInfo": {
                    "afterServiceTelephoneNumber": "010-0000-0000",
                    "afterServiceGuideContent": "상품 문의는 스토어 채팅으로 연락 주세요.",
                },
                "originAreaInfo": {
                    "originAreaCode": "0200037",  # 기타국가
                },
                "minorPurchasable": True,
                "productInfoProvidedNotice": {
                    "productInfoProvidedNoticeType": "ETC",
                    "etc": {
                        "itemName": product["name_kr"],
                        "modelName": product["name_kr"],
                        "manufacturer": "상세페이지 참조",
                        "certificateDetails": "상세페이지 참조",
                        "weight": "상세페이지 참조",
                        "size": "상세페이지 참조",
                        "components": "상세페이지 참조",
                        "relatedLegalConfirmation": "N",
                        "importDeclaration": "N",
                        "brandCountryOfOrigin": "상세페이지 참조",
                        "qualityAssuranceStandard": "상세페이지 참조",
                        "customerServicePhoneNumber": "010-0000-0000",
                    }
                },
                "tags": [{"text": t} for t in product.get("tags", [])],
            },
        },
        "smartStoreChannelProduct": {
            "naverShoppingRegistration": True,
            "channelProductDisplayStatusType": "ON",
            "storeKeepExclusiveProduct": False,
        }
    }

    resp = requests.post(
        "https://api.commerce.naver.com/external/v2/products",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=30,
    )

    if resp.status_code not in (200, 201):
        raise Exception(f"등록 실패 [{resp.status_code}]: {resp.text[:500]}")

    return resp.json()


# ─────────────────────────────────────────────
# Streamlit UI
# ─────────────────────────────────────────────
st.set_page_config(page_title="누쿠모리샵 상품 등록", page_icon="🛍️", layout="wide")
st.title("🛍️ 누쿠모리샵 상품 자동 등록")
st.caption("타오바오 · 1688 · 아마존 JP → 스마트스토어")

# 사이드바: 설정
with st.sidebar:
    st.header("⚙️ 설정")
    st.success("누쿠모리샵 API 키 연결됨 ✓")
    margin = st.slider("마진 배율 (원가 ×)", 1.0, 5.0, float(MARGIN_MULTIPLIER), 0.1)
    category_id = st.text_input("카테고리 ID", value="50000803")
    st.caption("[카테고리 조회](https://apicenter.commerce.naver.com) 후 입력")
    MARGIN_MULTIPLIER = margin

# 메인: URL 입력
url = st.text_input("🔗 상품 URL 붙여넣기",
    placeholder="https://item.taobao.com/... 또는 https://detail.1688.com/... 또는 https://www.amazon.co.jp/...")

col1, col2 = st.columns([1, 4])
fetch_btn = col1.button("📦 상품 가져오기", use_container_width=True)

if fetch_btn and url:
    site = detect_site(url)
    if site == "unknown":
        st.error("지원하지 않는 사이트입니다. 타오바오/1688/아마존JP URL을 입력해 주세요.")
    else:
        with st.spinner(f"[{site}] 상품 정보 수집 중..."):
            try:
                product = asyncio.run(scrape_product(url))
                # 가격 sanity check
                if product.get("price_krw", 0) > 1000000:
                    product["price_krw"] = 0
                    product["price_original"] = 0
                    st.warning("⚠️ 가격 자동 추출 실패 — 직접 입력해 주세요.")
                st.session_state["product"] = product
                st.success("수집 완료!")
            except Exception as e:
                st.error(f"스크래핑 실패: {e}")

if "product" in st.session_state:
    product = st.session_state["product"]

    st.divider()
    st.subheader("📋 수집된 정보")

    col_img, col_info = st.columns([1, 2])

    with col_img:
        imgs = product.get("images", [])
        if imgs:
            st.image(imgs[0], caption="대표 이미지", use_container_width=True)
        st.caption(f"이미지 {len(imgs)}장")

    with col_info:
        st.markdown(f"**원본 상품명:** {product['name']}")
        price_label = f"{product['price_original']} {product['currency']} → ₩{product['price_krw']:,}"
        st.markdown(f"**원가:** {price_label}")
        sale_price = calculate_sale_price(product["price_krw"], MARGIN_MULTIPLIER)
        st.markdown(f"**판매가 (×{MARGIN_MULTIPLIER}):** ₩{sale_price:,}")

    # 수집 즉시 자동 번역
    if "translated" not in product:
        with st.spinner("🌐 Google 번역 중..."):
            product = translate_to_korean(product)
            st.session_state["product"] = product
            st.rerun()
    else:
        st.divider()
        st.subheader("✏️ 등록 정보 수정")

        name_kr = st.text_input("상품명 (한국어)", value=product["name_kr"])
        summary = st.text_input("요약", value=product.get("summary", ""))
        tags_input = st.text_input("태그 (쉼표 구분)", value=", ".join(product.get("tags", [])))
        custom_price_str = st.text_input("판매가 (원)", value=str(sale_price))
        try:
            custom_price = int(re.sub(r"[^\d]", "", custom_price_str) or 0)
        except Exception:
            custom_price = sale_price

        product["name_kr"] = name_kr
        product["summary"] = summary
        product["tags"] = [t.strip() for t in tags_input.split(",") if t.strip()]
        product["final_price"] = custom_price
        st.session_state["product"] = product

        st.divider()
        if st.button("🚀 스마트스토어에 등록하기", type="primary", use_container_width=True):
            if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
                st.error("네이버 Client ID/Secret을 사이드바에 입력해 주세요.")
            else:
                with st.spinner("스마트스토어 등록 중..."):
                    try:
                        # 가격을 수정된 값으로 업데이트
                        product["price_krw"] = int(custom_price / MARGIN_MULTIPLIER)
                        result = register_product_naver(product, category_id)
                        prod_no = result.get("originProductNo", "")
                        channel_no = result.get("smartStoreChannelProductNo", "")
                        st.success(f"✅ 등록 완료! 상품번호: {prod_no} / 채널번호: {channel_no}")
                        st.json(result)
                        del st.session_state["product"]
                    except Exception as e:
                        st.error(f"등록 실패: {e}")
