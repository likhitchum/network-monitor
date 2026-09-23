# WinsWiew Network Monitor (WWNM)

Self-hosted network monitoring dashboard (แนวทางเดียวกับ PRTG / UptimeRobot) — backend FastAPI + SQLite, frontend vanilla JS บน nginx, deploy ด้วย Docker Compose

![CI](https://img.shields.io/github/actions/workflow/status/likhitchum/network-monitor/ci.yml?branch=main&label=CI)
![Coverage](https://img.shields.io/codecov/c/github/likhitchum/network-monitor/main)

> ⚠️ แก้ `likhitchum/network-monitor` ใน badge ด้านบนเป็นที่อยู่ GitHub จริงหลัง push ขึ้น remote

## คุณสมบัติ

- **Monitors**: HTTP/HTTPS (พร้อมตรวจ SSL expiry), Ping, VPN gateway, SNMP v2c (CPU / RAM / Disk sensors)
- **Per-device interval** — แต่ละเครื่องตั้งความถี่เช็คของตัวเองได้ (15–86400 วินาที) โดย scheduler เช็คขนานพร้อมจำกัด concurrency
- **Alert engine**: device down, HTTP status, SSL ใกล้หมดอายุ, SNMP resource threshold — **auto-resolve** เมื่อกลับมาปกติ ไม่สร้าง alert ซ้ำ
- **Notifications**: Email (SMTP 587/STARTTLS หรือ 465/implicit TLS) + Webhook
- **Topology maps** แยกตามไซต์งาน (drag & drop layout, บันทึกตำแหน่ง), รายงาน availability 30 วัน
- **Bilingual UI** ไทย/อังกฤษ + role-based access (admin / user)

## สถาปัตยกรรมระบบ

```
                 ┌────────────────────────────────────────────┐
   Browser ─────►│  nginx (frontend, :8080)                   │
                 │  • static: index.html / js/* / styles.css│
                 │  • proxy /api/* → networkpulse-api:8000    │
                 │  • security headers, cache policy          │
                 └──────────────┬─────────────────────────────┘
                                │
                 ┌──────────────▼─────────────────────────────┐
                 │  FastAPI (backend, :8000)                  │
                 │  • REST API — JWT (HS256, 8h) + roles      │
                 │  • validate_config (production guard)      │
                 │  • lifespan: start/cancel background loops │
                 │                                            │
                 │  Background loops:                         │
                 │  ① scheduler_loop  ── tick ทุก 5s ─┐       │
                 │  ② metrics_retention_loop (รายวัน) │       │
                 │        ┌───────────────────────────┘       │
                 │        ▼ due devices (interval ต่อเครื่อง) │
                 │  check_due_devices: gather + Semaphore(10) │
                 │        ▼                                   │
                 │  perform_check → persist_result → alerts   │
                 │  probes: httpx / ping / snmpget / ssl      │
                 │  notify: SMTP (587|465) / webhook          │
                 └──────────────┬─────────────────────────────┘
                                │
                 ┌──────────────▼─────────────────────────────┐
                 │  SQLite (/data/networkpulse.db)            │
                 │  PRAGMA foreign_keys=ON + indexes          │
                 │  users · devices · metrics · alerts        │
                 │  settings · topology_maps/_devices/_nodes  │
                 └────────────────────────────────────────────┘
```

**Data flow ของการเช็คหนึ่งรอบ:** scheduler ตื่นทุก `CHECK_TICK_SECONDS` → อ่าน device ที่ enabled → ตัวที่ถึงคิวตาม `interval_seconds` ของตัวเองถูกยิงผ่าน `asyncio.gather` (จำกัดด้วย `MAX_CONCURRENT_CHECKS`) → `perform_check` วัดผลจริง → `persist_result` บันทึก metrics + อัปเดตสถานะ + เปิด/ปิด alert ตาม threshold → ส่ง notification เฉพาะ alert ที่เพิ่งเกิดใหม่

**การเก็บรักษาข้อมูล:** metrics มี index `(device_id, checked_at)` และถูกลบอัตโนมัติเกิน `METRICS_RETENTION_DAYS` โดย retention loop

## เริ่มใช้งาน — Production (Docker Compose)

```bash
cp .env.example .env
# แก้ค่าสำคัญใน .env ก่อนขึ้นจริง (ดูตารางด้านล่าง)
docker compose up -d --build
# เปิด http://localhost:8080
```

บัญชีเริ่มต้นคือ `ADMIN_EMAIL` / `USER_EMAIL` ใน `.env` (เข้าสู่ระบบผ่านหน้า login; ปุ่ม demo accounts ควรถูกถือว่าใช้เฉพาะ dev)

**ข้อบังคับ production** — `validate_config()` จะ **ปฏิเสธ start** ถ้า `APP_ENV=production` แต่:

- `APP_SECRET` สั้นกว่า 32 ตัวอักษร หรือเป็นค่า default/dev
- `ADMIN_PASSWORD` / `USER_PASSWORD` สั้นกว่า 12 ตัวอักษร หรืออยู่ใน blacklist
- `ALLOWED_ORIGINS` ว่าง หรือใช้ `*`

ข้อมูลถาวรเก็บใน Docker volume `networkpulse-data` (backup ด้วยการ copy `/data/networkpulse.db`)

## การรันแบบ Development

### Backend เดี่ยว (hot reload + Swagger)

ต้องมี Python 3.12+ และ `ping` / `snmpget` (บน Debian/Ubuntu: `apt install iputils-ping snmp`)

```bash
cd backend
pip install -r requirements.txt -r requirements-dev.txt

export DATABASE_PATH=./dev.db          # Windows PowerShell: $env:DATABASE_PATH="./dev.db"
export APP_SECRET=dev-secret-0123456789abcdef0123456789abcdef
export ADMIN_PASSWORD=dev-admin-pass-1
export USER_PASSWORD=dev-user-pass-12

uvicorn app.main:app --reload --port 8000
# Swagger UI: http://localhost:8000/docs
```

ไม่ตั้ง `APP_ENV=production` ใน dev ก็จะไม่มีการบังคับความปลอดภัย (แต่ `APP_SECRET` ควรตั้งเองไว้เสมอ)

### Frontend + ทั้งระบบ พร้อม hot reload

สร้างไฟล์ `docker-compose.override.yml` (compose จะ merge ให้อัตโนมัติ) เพื่อ bind-mount ไฟล์ static แล้วแก้โค้ดได้โดยไม่ต้อง rebuild:

```yaml
services:
  network-monitor:
    volumes:
      - ./index.html:/usr/share/nginx/html/index.html
      - ./js:/usr/share/nginx/html/js
      - ./styles.css:/usr/share/nginx/html/styles.css
  networkpulse-api:
    environment:
      APP_ENV: development
```

แก้ Python code → uvicorn reload เอง (ภายใน container ใช้ `--reload` ไม่ได้เพราะ CMD ตายตัว จึงเหมาะกับรัน backend เดี่ยวด้านบน หรือ restart container) แก้ JS/HTML/CSS → รีเฟรช browser ได้เลย

## ตัวแปร environment ทั้งหมด

| ตัวแปร | Default | ความหมาย |
|---|---|---|
| `APP_SECRET` | `dev-only-change-me` | JWT signing key (**≥32 ตัวอักษรใน production**) |
| `APP_ENV` | `development` | ตั้ง `production` เพื่อเปิด config guard |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | `admin@example.com` / `change-me` | บัญชี admin seed |
| `USER_EMAIL` / `USER_PASSWORD` | `user@example.com` / `user123` | บัญชี user seed |
| `ALLOWED_ORIGINS` | `http://localhost:8080` | CORS whitelist (comma-separated) |
| `DATABASE_PATH` | `/data/networkpulse.db` | ตำแหน่งไฟล์ SQLite |
| `POLL_INTERVAL_SECONDS` | `60` | **ค่า floor ของ interval ต่ออุปกรณ์** |
| `CHECK_TICK_SECONDS` | `5` | ความละเอียดของ scheduler tick |
| `MAX_CONCURRENT_CHECKS` | `10` | จำนวน check พร้อมกันสูงสุด |
| `LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `METRICS_RETENTION_DAYS` | `30` | อายุข้อมูล metrics (ระบบลบเกินเอง) |
| `RETENTION_SWEEP_HOURS` | `24` | ความถี่ของ retention sweep |

> SMTP / Webhook ตั้งผ่านหน้า Settings ในเว็บ (เก็บในตาราง `settings`) — env ฝั่ง SMTP ไม่ถูกอ่าน

## การทดสอบ

### โครงสร้างชุดทดสอบ (119 เคส, coverage 93%)

| ไฟล์ | เคส | ครอบคลุม |
|---|---|---|
| `tests/test_auth.py` | 8 | login, token, roles, protected routes |
| `tests/test_devices.py` | 13 | CRUD, enable/disable, สิทธิ์, validation |
| `tests/test_checks.py` | 8 | endpoint check, metrics, อัปเดตสถานะ |
| `tests/test_alerts.py` | 11 | alert สร้าง/dedupe/auto-resolve, SSL, SNMP threshold |
| `tests/test_settings.py` | 11 | thresholds, SMTP (รวม regression กัน password โดนลบ) |
| `tests/test_maps.py` | 16 | maps CRUD, layout, hide/re-add, legacy topology, cascade |
| `tests/test_retention.py` | 5 | sweep metrics เกินกำหนด, loop ทน DB error |
| `tests/test_lifespan.py` | 3 | start/cancel background loops, init_db |
| `tests/test_perform_check.py` | 20 | probes จริง (mock httpx/subprocess/SSL) ทุกประเภท |
| `tests/test_scheduler.py` | 11 | interval ต่ออุปกรณ์ (fake clock), semaphore concurrency จริง |
| `tests/test_validate_config.py` | 13 | กฎ production ทุกข้อ + boundary |

ชุดทดสอบเป็น **hermetic** ทั้งหมด — stub `perform_check`, fake `subprocess`/SSL, temp SQLite — รันแล้วไม่มี packet หลุดออกนอกเครื่อง และเขียนตามแนวปฏิบัติ: fixture ใน `conftest.py` ให้ `client` (httpx ASGI), token แอดมิน/ยูสเซอร์, `create_device` helper และ fixture `perform_check_result` สำหรับควบคุมผล check

### รันบนเครื่อง

```bash
cd backend
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest                    # รันทั้งหมด + coverage report ท้ายผลลัพธ์
python -m pytest tests/test_maps.py # รันเฉพาะไฟล์
```

### รันใน container (สภาพแวดล้อมเดียวกับ CI)

```bash
docker run --rm -v "$PWD/backend:/src" -w /src python:3.12-slim \
  bash -c "pip install -q -r requirements.txt -r requirements-dev.txt && python -m pytest"
```

### CI (GitHub Actions — ทุก push / PR)

1. **pytest job** — Python 3.12 (cache pip) → รัน tests พร้อม `--cov-fail-under=75` (ปัจจุบัน 93%) → อัปโหลด `coverage.xml` เป็น artifact + ไป Codecov
2. **docker-build job** (`needs: pytest`) — build ทั้ง frontend และ backend image (`push: false`, cache type=gha) ยืนยันว่า Dockerfile build ผ่านเสมอ
3. **smoke-test job** (`needs: pytest`) — `docker compose up -d --build --wait` ประกอบ stack จริง → ยิง `/api/health` ผ่าน nginx → login end-to-end → `down -v` เก็บกวาด

## การป้องกัน branch `main` (Branch protection)

หลัง push ขึ้น GitHub แล้ว แนะนำล็อก `main` ให้ merge ได้เฉพาะเมื่อ CI ผ่านทุก job — เลือกตั้งอย่างใดอย่างหนึ่งจากสองวิธี:

### วิธีที่ 1: Branch protection rules (คลาสสิก)
1. repo → **Settings** → **Branches** (หมวด "Rules")
2. **Add branch protection rule**
3. **Branch name pattern**: `main`
4. เปิดตัวเลือก:
   - ✅ **Require a pull request before merging** — ปฏิเสธ push ตรงเข้า main (repo คนเดียวที่ไม่อยาก review ตัวเอง ข้ามข้อนี้ได้ แต่จะเหลือแค่กัน merge ตอน CI แดง)
   - ✅ **Require status checks to pass before merging** → ค้นและเลือก 3 check: `pytest`, `docker-build`, `smoke-test` (ชื่อ = job id ใน `.github/workflows/ci.yml`)
   - ✅ **Require branches to be up to date before merging** — กัน merge ทับ commit ใหม่ที่ CI ยังไม่ได้เทส
   - (แนะนำ) ✅ **Require conversation resolution before merging**
5. **Create** — เสร็จ ทดสอบ: ลองแก้ไฟล์ทำ test พังแล้วเปิด PR ปุ่ม Merge จะล็อก

### วิธีที่ 2: Rulesets (ใหม่กว่า, GitHub แนะนำ)
1. **Settings** → **Rules** → **Rulesets** → **New ruleset** → **Branch ruleset**
2. ชื่อ เช่น `protect-main` + **Enforcement status: Active**
3. **Target branches** → Add a target → ชื่อ branch: `main`
4. เปิด **Require status checks to pass** → เพิ่ม `pytest`, `docker-build`, `smoke-test`
5. (ทางเลือก) เปิด **Require a pull request before merging** + **Block force pushes** → **Create ruleset**

ข้อแตกต่างที่ควรรู้: Rulesets มี **Bypass list** ระบุชัดว่าใคร (เช่น admin) ข้ามกฎได้ และใช้กับ tag ได้ด้วย — ถ้าตั้งทั้งสองแบบซ้อนกันกฎจะบังคับรวมกันทั้งคู่ จึงควรเลือกอย่างใดอย่างหนึ่ง

> 💡 ถ้าลิสต์ status checks ยังว่างเปล่า: GitHub แสดงเฉพาะ check ที่รันบน repo ภายใน ~7 วัน — push (หรือ re-run workflow) อย่างน้อยหนึ่งรอบก่อน แล้วค่อยกลับมาเลือก

## Development Guide

### โครงสร้างไฟล์

```
├── index.html                 # โครงหน้าเว็บ (login, dashboard, modal) + cache-busting ?v=
├── js/                        # native ES modules ฝั่ง frontend (ไม่มี bundler)
│   ├── main.js                # จุดเริ่ม (entry) — wire controls + initial loads
│   ├── api.js                 # apiFetch + session storage (Bearer token, 401 handling)
│   ├── utils.js               # escapeHtml / safeClass / deviceStatus
│   ├── toast.js               # toast notifications
│   ├── devices.js             # device list state + table + check-now
│   ├── alerts.js              # alertRowHTML helper + dashboard/alerts/notifications
│   ├── maps.js                # topology editor + drag + layout persistence
│   ├── summary.js             # dashboard stats + donut
│   ├── reports.js             # report view data
│   ├── settings.js            # thresholds + SMTP handlers
│   ├── i18n.js                # TH/EN translation maps + language switching
│   ├── views.js               # view templates + view router
│   └── auth.js                # login/logout + session restore ผ่าน /api/me
├── styles.css
├── nginx.conf                 # static + proxy /api/* → backend + security headers
├── Dockerfile                 # frontend image (nginx:1.27-alpine)
├── docker-compose.yml
├── .env.example
└── backend/
    ├── Dockerfile             # backend image (python:3.12-slim + iputils-ping + snmp)
    ├── requirements.txt       # runtime deps (ติดตั้งใน image จริง)
    ├── requirements-dev.txt   # pytest/pytest-asyncio/pytest-cov เฉพาะ dev
    ├── pytest.ini             # asyncio_mode=auto + coverage config
    ├── app/
    │   └── main.py            # backend ทั้งหมดอยู่ในไฟล์เดียว (~770 บรรทัด)
    └── tests/
        ├── conftest.py        # fixtures กลาง (client, tokens, create_device, stub)
        └── test_*.py          # 11 โมดูล 119 เคส
```

### สถาปัตยกรรม frontend (ES modules)

Frontend เป็น native ES modules ไม่มี bundler — index.html โหลด entry เดียวคือ `js/main.js?v=15` ที่เหลือถูกดึงผ่าน `import` โครง dependency เป็นชั้น ๆ **ห้ามวนกลับ** (วงจร = SyntaxError ทำหน้าทั้งหน้าขาด):

```
utils.js · api.js · toast.js           ← leaf: ห้าม import โมดูลอื่นใน js/
        ▲
devices · alerts · maps · summary · reports · settings · i18n
        ▲
views.js (view templates + router) · auth.js (login / restore session)
        ▲
main.js — wire events + initial loads
```

กติกาเวลาแก้ frontend:

1. **API ทุก call ต้องผ่าน `apiFetch()`** (js/api.js) — แนบ Bearer token จาก session storage (`networkpulse-session`), parse JSON, throw `Error(data.detail)` เมื่อ response ไม่ ok และจัดการ 401 เอง (เคลียร์ session + reload เฉพาะเมื่อ app shell แสดงอยู่ — กันลูป reload ตอนยังอยู่หน้า login) — **ห้ามเรียก `fetch()` ตรง ๆ และห้าม monkey-patch**
2. **XSS: ข้อมูลจาก API ทุกชิ้นก่อนเข้า HTML ต้องผ่าน `escapeHtml()`** และค่าที่เป็น class attribute ต้องผ่าน `safeClass(value, allowed, fallback)` (js/utils.js) — ห้าม interpolate ตรง ๆ แม้แต่ id/ชื่อ
3. **Template ซ้ำหลอมเป็น helper เดียว** — แถว alert ทั้ง 3 ที่ (dashboard / หน้า Alerts / notification popover) render ผ่าน `alertRowHTML(alert, {variant, resolveButton, arrow})` จาก js/alerts.js เท่านั้น — escape อยู่ที่เดียวครอบทุกจุด แก้ครั้งเดียวมีผลทั้ง 3 ที่ ห้าม copy markup
4. **สถานะอุปกรณ์มาจาก `deviceStatus()` จุดเดียว** — mapping backend status → badge (`online/warning/critical/paused/unknown`) ทั้งตารางและแผนผังต้องเรียกตัวเดียวกัน ไม่งั้นสองที่เห็นสถานะไม่ตรงกัน
5. **แต่ละโมดูลถือ state ของตัวเอง** — เช่น `devices` อยู่ใน devices.js, `mapNodes`/`currentMapId` อยู่ใน maps.js — โมดูลอื่นเข้าถึงผ่าน exported functions เท่านั้น และทุกชื่อใน `export {}` ต้องมีการประกาศจริงในไฟล์ (ลืมประกาศ = `Export 'x' is not defined` พังทันทีตั้งแต่โหลด)
6. **เพิ่ม feature ใหม่** — สร้าง `js/<feature>.js` (state + fetch + render ในไฟล์เดียว) → เพิ่ม template ใน `viewContent` ของ views.js → wire event ใน `openView()` หรือ main.js — import ให้ครบทุกตัวที่ใช้ (บั๊กล่าสุดคือเรียก `showToast` โดยไม่ import)
7. **แก้ JS/CSS แล้ว bump `?v=`** ใน index.html (`js/main.js?v=` และ `styles.css?v=`) — นี่คือกลไก cache-busting เดียวของระบบ (nginx เสิร์ฟ static แบบ cache ยาว)
8. **ตรวจก่อน commit** — syntax ทุกไฟล์ที่แก้: `node --check js/<file>.js` หรือครบชุดใน container:

   ```bash
   docker run --rm -v "$PWD/js:/app" -w /app node:22-alpine sh -c "for f in *.js; do node --check \$f; done"
   ```

   ส่วนพฤติกรรมยืนยันด้วย smoke แบบเดียวกับ CI job `smoke-test` (compose ขึ้นจริง + login E2E) — frontend ยังไม่มี unit test ของตัวเอง

### วิธีเพิ่ม monitor type ใหม่ (ตัวอย่าง: TCP port check)

ยกตัวอย่างเพิ่ม type `tcp` — แตะ 4 จุด:

**1. เปิด type ใน schema** (`backend/app/main.py`) — เพิ่มลง regex ทั้งสอง model:

```python
class DeviceCreate(BaseModel):
    monitor_type: str = Field(pattern="^(http|https|ping|vpn|snmp|tcp)$")   # เพิ่ม |tcp
class DeviceUpdate(BaseModel):
    monitor_type: str | None = Field(default=None, pattern="^(http|https|ping|vpn|snmp|tcp)$")
```

**2. เพิ่ม branch ใน `perform_check()`** — ต้องคืน dict ตาม shape เดิม:

```python
elif device["monitor_type"] == "tcp":
    port = int(device.get("tcp_port") or 80)          # ถ้าต้องการ field ใหม่ อย่าลืม migration ใน init_db
    try:
        await asyncio.to_thread(socket_create_connection, host, port, 5)
        result["status"] = "up"
    except OSError:
        raise RuntimeError(f"TCP connect to {host}:{port} failed")
```

ข้อบังคับของผลลัพธ์: `status` เป็นหนึ่งใน `up|warning|down|unknown` (exception ใด ๆ ใน try = `down` อัตโนมัติจาก catch ด้านนอก), `response_ms` ถูกวัดให้เองท้ายฟังก์ชัน, ส่วนที่เหลือ (`status_code/ssl_days_left/error/sensors`) ใส่ได้ตามความเหมาะสม — `persist_result()` จะจัดการ alert + notification ต่อให้เอง

**3. ฝั่ง frontend** (`js/` แบบ ES modules — โครง dependency และกติกาดู "สถาปัตยกรรม frontend" ด้านบน) — แตะ 3 จุด: option ใน select ของ form (index.html), `typeMap` ใน submit handler ของ js/main.js และไอคอนใน `deviceView()` ของ js/devices.js (ถ้ามี sensor ให้เติมการแสดงผลใน `sensorSummary()` ไฟล์เดียวกัน)

```js
// deviceView(): เลือกไอคอน (server/switch/router)
type: ...device.monitor_type==='tcp' ? 'router' : ... 
// deviceForm typeMap: label ใน dropdown → type
const typeMap = { 'Ping only':'ping', 'VPN gateway':'vpn', 'SNMP v2c':'snmp', 'HTTP / HTTPS':'https', 'TCP port':'tcp' };
```

**4. เทส** — เพิ่มเคสใน `tests/test_perform_check.py` (mock ทุกอย่างที่ออกนอกเครื่อง — ดูแนวปฏิบัติด้านล่าง) และเคส validation ใน `tests/test_devices.py` แล้วรัน `python -m pytest` ให้ครบเขียว

> ถ้า monitor ใหม่ต้องใช้ binary เพิ่ม (เช่น `nc`, `nmap`) ให้เพิ่มใน `apt-get install` ของ `backend/Dockerfile` ด้วย ไม่งั้น CI docker-build job จะยังผ่านแต่ probe พังตอน runtime

### แนวปฏิบัติการเขียน test

1. **Hermetic เสมอ** — ห้ามยิง network จริง: ผล check ควบคุมผ่าน fixture `perform_check_result` (autouse stub), ถ้าต้องทดสอบ probe จริงให้ mock `m.httpx` / `m.subprocess` / SSL ตามแบบใน `tests/test_perform_check.py`
2. **ใช้ fixtures จาก conftest ให้คุ้ม** — `client` (httpx ASGI), `admin_token`/`user_token`/`auth_headers`, `create_device(**overrides)`; อย่า login/sync สร้างเองซ้ำ
3. **DB ใช้ร่วมทั้ง session** — เป็น temp SQLite ไฟล์เดียว: กรอง assertion ด้วย device id ของตัวเอง (เช่น `next(item for item in rows if item["id"] == device["id"])`) และ assert แบบ superset (`>=`) เมื่อรายการมีของ test อื่นปนได้; ถ้าสร้าง row แปลกให้ลบใน `finally`
4. **ทดสอบ background loop ด้วย fake clock** — monkeypatch `m.time.monotonic` + `asyncio.sleep` ให้เลื่อนเวลาแทนการรอจริง (ดู `tests/test_scheduler.py`); จบ loop ด้วยการ raise `CancelledError` จาก fake sleep ให้ deterministic
5. **ต้องการทดสอบ pipeline จริงทะลุ stub** — restore ตัวจริงเฉพาะจุด: `monkeypatch.setattr(m, "perform_check", real_perform_check)` (ตัวอย่าง: SNMP threshold end-to-end)
6. **รักษาให้ pytest เงียบ** — `pytest.ini` กรองเฉพาะ warning ของ third-party; ห้ามเพิ่มโค้ดที่ emit warning ใหม่ (DeprecationWarning ของ FastAPI/PyJWT ถูกกำจัดหมดแล้ว — อย่าให้กลับมา)
7. **ตั้งชื่อและจัดไฟล์ตามพื้นที่** — `test_<พื้นที่>.py`, helper ภายในไฟล์นำหน้าด้วย `_`; เคสใหม่ที่จับ regression ให้เขียน comment บอกที่มา (ตัวอย่าง: `test_hide_node_unknown_map_or_device_returns_404`)

## Security notes

- รหัสผ่าน: PBKDF2-SHA256 310k iterations + constant-time compare
- JWT อายุ 8 ชั่วโมง, CORS whitelist, nginx security headers, SQL parameterized ทั้งหมด
- Frontend escape HTML ทุกจุดที่ render ข้อมูลจาก API (`escapeHtml` + `safeClass` whitelist สำหรับ class attributes)
- SMTP password เก็บใน DB แต่ไม่เคยถูกส่งกลับไปแสดง และการบันทึกฟอร์มด้วยรหัสผ่านว่างจะคงค่าเดิมไว้
- FK constraints เปิดทุก connection พร้อม orphan cleanup ตอน startup
