# Bill of Lading Contact Portal

Single-user local Linux web portal (no login/password) to:
- upload bill of lading CSV data
- search companies (shipper/consignee/importer/exporter/sender/receiver)
- search by HS/HX code
- collect categorized contact info (sender/receiver/importer/exporter/other)
- collect contact hints (email/phone/website/address + bill number + HS/HX code)
- enrich from free sources (ImportYeti + OpenCorporates API)
- serve through Nginx on `info.adminoabc.org`

## Data sources
- OEC bulk download (manual CSV upload): https://oec.world/en/resources/bulk-download/bill-of-lading
- ImportYeti search scraping (best effort): https://www.importyeti.com
- OpenCorporates (free public company registry data): https://api.opencorporates.com

## Quick start (local Linux)

```bash
cd /path/to/project
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

Open: `http://<server-ip>:8080`

For production-style serving behind Nginx, run with Gunicorn:

```bash
source .venv/bin/activate
pip install gunicorn
gunicorn --bind 127.0.0.1:8080 app:app
```

## Nginx auto setup for domain forwarding

```bash
cd /path/to/project
chmod +x scripts/setup_nginx.sh
sudo ./scripts/setup_nginx.sh info.adminoabc.org 8080
```

Then point DNS A record for `info.adminoabc.org` to your server IP.

## Notes
- Contacts are aggregated from raw shipping rows + free web sources; always verify manually before outreach.
- ImportYeti page structure may change; enrichment is best effort and intentionally low-cost.
- Sender/receiver contacts are inferred from BOL columns and shown in categorized searchable format.
