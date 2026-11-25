# Copyright (c) 2026 EazyWhale
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Automated Whole-Page Regression & Anti-FP Self-Test untuk --check-config.
Menguji:
1. Jebakan False Positive halaman utuh (RSUD, Klinik, Toko Permen/Retail, Portal Berita Dewan Pers,
   Platform Komik/Streaming Legal Webtoon/Vidio) -> wajib VETO_LEGAL / REJECT.
2. Target Positif halaman utuh (Komik ilegal, Film ilegal, Porn, Judol, Phishing) -> wajib ACCEPT.
3. Situs Target dengan <title>/<header> disamarkan + Adaptive +1 Sample Page Probe -> wajib ACCEPT.
"""
from __future__ import annotations

from .config_loader import IndexerConfig
from .inspect.dom_parser import parse_html_zones
from .inspect.gatekeeper import decide_gate_verdict, needs_adaptive_probe

BENIGN_TRAPS: list[tuple[str, str]] = [
    (
        "rsudpasarminggu.go.id",
        '''<html><head><title>RSUD Pasar Minggu - Jadwal Dokter & Pendaftaran Online</title>
        <script type="application/ld+json">{"@context":"https://schema.org","@type":"Hospital","name":"RSUD Pasar Minggu"}</script>
        </head><body>
        <nav><a href="/jadwal-dokter">Jadwal Dokter</a><a href="/layanan">Layanan</a><a href="/igd">IGD 24 Jam</a></nav>
        <main><h1>Pendaftaran Pasien Rawat Jalan</h1>
        <p>Silakan pilih slot jadwal dokter spesialis Anda. Layanan BPJS Kesehatan & Kementerian Kesehatan.</p></main>
        <footer>© 2026 RSUD Pasar Minggu. Jl. TB Simatupang No. 1 Jakarta Selatan. Telp: 021-29059999</footer>
        </body></html>''',
    ),
    (
        "apollohospitals.com",
        '''<html><head><title>Apollo Hospitals - Book an Appointment & Health Checkup</title>
        <script type="application/ld+json">{"@type":"MedicalClinic","name":"Apollo Hospitals"}</script>
        </head><body>
        <nav><a href="/doctors">Doctors</a><a href="/appointment">Appointment</a></nav>
        <main><h1>Medical Services & Specialist Doctors</h1>
        <p>Login to patient portal or register online for consultation and health checkup.</p></main>
        <footer>© 2026 Apollo Hospitals Enterprise Ltd. Phone: +91-44-28290200</footer>
        </body></html>''',
    ),
    (
        "americancandyandcoffee.com",
        '''<html><head><title>American Candy & Coffee - Artisan Sweets & Roastery</title>
        <script type="application/ld+json">{"@type":"Store","name":"American Candy and Coffee"}</script>
        </head><body>
        <nav><a href="/shop">Shop</a><a href="/cart">Cart</a><a href="/checkout">Checkout</a></nav>
        <main><h1>Fresh Roasted Coffee & Gourmet Candy</h1>
        <p>Add to cart and enjoy fast shipping. Nomor Induk Berusaha & Sertifikat Halal.</p></main>
        <footer>© 2026 PT Manis Kopi Nusantara. Jl. Kemang Raya No. 12 Jakarta. Tel: 021-7198821</footer>
        </body></html>''',
    ),
    (
        "situsberita-nasional.co.id",
        '''<html><head><title>Polri Gerebek Markas Judi Online Slot Gacor, Sita Rekening Deposit</title>
        <script type="application/ld+json">{"@type":"NewsMediaOrganization","name":"Berita Nasional"}</script>
        </head><body>
        <nav><a href="/nasional">Nasional</a><a href="/hukum">Hukum</a><a href="/redaksi">Redaksi</a>
        <a href="/pedoman-media-siber">Pedoman Media Siber</a></nav>
        <main><article><h1>Bareskrim Tangkap Tersangka Bandar Togel Online</h1>
        <p>Laporan wartawan terverifikasi Dewan Pers mengenai pemberantasan judi online dan pasal 303 KUHP.</p>
        </article></main>
        <footer>© 2026 PT Media Berita Nasional. Susunan Redaksi | Kode Etik Jurnalistik</footer>
        </body></html>''',
    ),
    (
        "webtoons.com",
        '''<html><head><title>LINE WEBTOON - Baca Komik Digital Resmi Gratis</title></head>
        <body>
        <nav><a href="/originals">Originals</a><a href="/pricing">Paket</a><a href="/subscribe">Langganan</a></nav>
        <main><h1>Komik Populer Resmi Berlisensi</h1>
        <p>Unduh di Google Play dan Download on the App Store. Hak cipta dilindungi undang-undang.</p></main>
        <footer>© 2026 WEBTOON Entertainment Inc. All rights reserved.</footer>
        </body></html>''',
    ),
]

POSITIVE_TARGETS: list[tuple[str, str, str]] = [
    (
        "komikcast-vip88.xyz",
        "comic",
        '''<html><head><title>Baca Komik & Manhwa Sub Indo Gratis Terbaru</title></head>
        <body>
        <nav><a href="/komik/solo-leveling">Daftar Komik</a><a href="/genre/isekai">Genre Isekai</a></nav>
        <main><h1>Update Chapter Terbaru Bahasa Indonesia</h1>
        <a href="/chapter-1">Chapter 1</a> <a href="/chapter-2">Chapter 2</a>
        <p>Baca manhwa sub indo gratis mirror 1 mirror 2 colored.</p>
        <a href="https://slotgacor88.xyz/daftar"><img src="/ads/slot-gacor.gif" alt="Slot Gacor Maxwin"></a>
        </main></body></html>''',
    ),
    (
        "lk21nonton-hd21.xyz",
        "film",
        '''<html><head><title>Nonton Film Sub Indo Gratis HD 1080p - LK21</title></head>
        <body>
        <nav><a href="/nonton/avatar-2">Nonton Film</a><a href="/quality/bluray">BluRay</a></nav>
        <main><h1>Streaming Film Bioskop Sub Indo Gratis</h1>
        <p>Pilih Server 1, Server 2, Ganti Player, Resolusi 1080p WEBRip, Download Batch Google Drive.</p>
        <iframe src="https://embed-player-mirror.cfd/v/12345"></iframe>
        <a href="https://togelvip77.top"><img src="/banner/rtp-live.gif" alt="RTP Live Slot"></a>
        </main></body></html>''',
    ),
    (
        "slotgacor-maxwin88.vip",
        "judol",
        '''<html><head><title>Situs Slot Gacor Hari Ini Gampang Maxwin & RTP Live</title></head>
        <body>
        <nav><a href="/register">Daftar</a><a href="/login">Login</a><a href="/rtp">RTP Live</a></nav>
        <main><h1>Bocoran Pola Gacor Anti Rungkad Pragmatic Play & PG Soft</h1>
        <p>Gates of Olympus, Starlight Princess, Mahjong Ways. Deposit Pulsa Tanpa Potongan, DANA, OVO, QRIS, BCA, BRI.
        Withdraw kilat pasti dibayar! Livechat 24 jam.</p>
        <a href="https://wa.me/628123456789">Daftar Sekarang</a>
        </main></body></html>''',
    ),
    (
        "bokepindo-viral18.icu",
        "porn",
        '''<html><head><title>Nonton Bokep Indo Viral Terbaru & JAV Sub Indo Gratis</title></head>
        <body>
        <nav><a href="/jav/sub-indo">JAV Sub Indo</a><a href="/bokep/viral">Bokep Viral</a></nav>
        <main><h1>Streaming Video Dewasa 18+ Only Tanpa Sensor</h1>
        <p>Adult content uncensored HD video free porn.</p>
        <iframe src="https://adult-stream-cdn.xyz/embed/999"></iframe>
        <a href="https://chaturbate.com/in/?tour=abc"><img src="/ads/live-sex.gif" alt="Live Sex"></a>
        </main></body></html>''',
    ),
    (
        "danakaget-klaim-resmi.xyz",
        "phishing",
        '''<html><head><title>Link Klaim Dana Kaget Hari Ini Resmi</title></head>
        <body>
        <main><h1>Formulir Verifikasi Data Sekarang & Klaim Saldo Gratis</h1>
        <form action="/verify" method="post">
        <input type="text" name="phone" placeholder="Nomor Handphone Terdaftar">
        <input type="password" name="pin" placeholder="Masukkan PIN & Kode OTP">
        <button>Lanjutkan Verifikasi</button>
        </form>
        <a href="https://api.telegram.org/bot12345/sendMessage">Bantuan</a>
        </main></body></html>''',
    ),
]


def run_self_test(cfg: IndexerConfig) -> bool:
    ok = True
    print(f"[SELF-TEST] Memverifikasi {len(cfg.categories)} kategori & {len(cfg.legal.profiles)} profil legal...")

    for dom, raw_html in BENIGN_TRAPS:
        zones = parse_html_zones(raw_html, f"https://{dom}/")
        verdict = decide_gate_verdict(cfg, dom, zones)
        if verdict.decision in ("ACCEPT", "REVIEW"):
            ok = False
            print(
                f"[SELF-TEST] ❌ FALSE POSITIVE pada {dom}: decision={verdict.decision} "
                f"(cat={verdict.category}, illegal={verdict.illegal_score}, legal={verdict.legal_score}, "
                f"reasons={verdict.reasons}, legal_reasons={verdict.legal_reasons})"
            )
    if ok:
        print(f"[SELF-TEST] ✓ 0 false positive pada {len(BENIGN_TRAPS)} jebakan entitas legal (RS, toko, berita, platform resmi).")

    pos_ok = True
    for dom, expected_cat, raw_html in POSITIVE_TARGETS:
        zones = parse_html_zones(raw_html, f"https://{dom}/")
        verdict = decide_gate_verdict(cfg, dom, zones, hint_cat=expected_cat)
        if verdict.decision != "ACCEPT" or verdict.category != expected_cat:
            ok = False
            pos_ok = False
            print(
                f"[SELF-TEST] ❌ GAGAL DETEKSI {dom} (target={expected_cat}): decision={verdict.decision}, "
                f"got={verdict.category}, pillars={verdict.pillars}, score={verdict.illegal_score}"
            )
    if pos_ok:
        print(f"[SELF-TEST] ✓ {len(POSITIVE_TARGETS)}/{len(POSITIVE_TARGETS)} situs target terdeteksi akurat.")

    disguised_dom = "portal-catatan-harian.xyz"
    disguised_home = '''<html><head><title>Catatan Harian & Koleksi Cerita</title></head>
    <body><main><h1>Selamat Datang di Arsip Digital</h1>
    <p>Klik untuk membaca chapter terbaru.</p>
    <a href="/chapter-1">Mulai Baca</a></main></body></html>'''
    disguised_sub = '''<html><head><title>Baca Komik & Manhwa Sub Indo Gratis</title></head>
    <body><main><h1>Chapter 1 Bahasa Indonesia</h1>
    <a href="/chapter-2">Next Chapter</a>
    <p>Baca manhwa sub indo gratis mirror 1 colored.</p>
    <a href="https://slotgacor88.xyz"><img src="/ads/slot.gif" alt="Slot Gacor Maxwin"></a>
    </main></body></html>'''

    z_home = parse_html_zones(disguised_home, f"https://{disguised_dom}/")
    probe_url = needs_adaptive_probe(cfg, disguised_dom, z_home, hint_cat="comic")
    if not probe_url.endswith("/chapter-1"):
        ok = False
        print(f"[SELF-TEST] ❌ Adaptive probe gagal memilih /chapter-1 (got={probe_url!r})")
    else:
        z_sub = parse_html_zones(disguised_sub, probe_url)
        v_final = decide_gate_verdict(cfg, disguised_dom, z_home, hint_cat="comic", secondary_zones=z_sub, probed_url=probe_url)
        if v_final.decision != "ACCEPT" or v_final.category != "comic":
            ok = False
            print(f"[SELF-TEST] ❌ Adaptive probe gagal membongkar situs tersamar: {v_final}")
        else:
            print("[SELF-TEST] ✓ Adaptive +1 sample page probe berhasil membongkar halaman depan yang disamarkan.")

    return ok
