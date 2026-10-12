# ProIklan + Firebase (percuma, pelan Spark)

Cara kerja: GitHub Actions ambil peraduan/promosi dari RSS setiap 6 jam dan tulis ke Firestore.
Laman web (GitHub Pages) baca dari Firestore. Anda lulus/buang melalui tab Admin.

## A. Firebase (sekali sahaja)
1. console.firebase.google.com > Add project > ProIklan. Kekal pelan Spark (percuma).
2. Build > Firestore Database > Create database > Production mode > region asia-southeast1.
3. Build > Authentication > Get started > Email/Password > Enable.
   Tab Users > Add user (emel + kata laluan anda). Salin **User UID**.
4. Firestore > Rules: tampal isi `firestore.rules` (sudah termasuk peraturan users dan groups), ganti `GANTI_UID` dengan UID tadi, tekan Publish.
5. Project settings (ikon gear) > Your apps > ikon Web `</>` > daftar app. Salin config ke `firebase-config.js`.

## B. Kunci untuk pengambil data
6. Project settings > Service accounts > Generate new private key. Satu fail JSON dimuat turun.
7. Di repo GitHub: Settings > Secrets and variables > Actions > New repository secret.
   Nama: `FIREBASE_SERVICE_ACCOUNT`. Nilai: tampal SELURUH isi fail JSON tadi.
   JANGAN letak fail JSON itu dalam repo atau kongsi kepada sesiapa.

## C. Hos laman
8. Muat naik semua fail ke repo (termasuk folder `.github`). Settings > Pages > Deploy from branch > main > / (root).
9. Actions > "Ambil data ProIklan" > Run workflow. Selepas ~1 minit, buka laman anda.
10. Tab Admin > log masuk dengan akaun langkah 3.

## Uji sumber di komputer sebelum itu (pilihan)
`python fetch_data.py --dry` papar apa yang dijumpai tanpa menulis apa-apa.

## Tambah sumber
Edit `sources.json`. Blog WordPress ada RSS di `https://namablog.com/feed/`. "type": "peraduan" atau "iklan".

## Had percuma
Firestore Spark: 50,000 bacaan sehari. Setiap lawatan membaca sehingga 80 dokumen, jadi sekitar 600 lawatan sehari.
Item lebih 30 hari dibuang automatik. Facebook/Instagram tidak boleh diambil.

## Laman tanpa RSS (contoh: ContestKaki)
Guna "format": "sitemap" dalam sources.json. Pengambil data baca sitemap.xml, ambil 15 halaman terbaru
(tajuk, ringkasan, gambar dari tag og:), dan menghormati robots.txt (laluan yang dilarang dilangkau).
Tapis halaman dengan "include" (regex URL), contoh: "include": "/contest/". Jeda 1 saat antara halaman.
Semak dulu dengan `python fetch_data.py --dry`.
