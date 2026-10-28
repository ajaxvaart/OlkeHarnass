# Installatie: OlkeHarnass met de assistent op apulinux

Deze stappen voert Olke zelf uit, als `auke`, in een terminal op apulinux. Regels met
`sudo` vragen om je wachtwoord. Elke stap is los terug te draaien (onderaan).

Wat er komt:

- een gebruiker `olkeharnass` zonder shell, niet in de groepen `docker` of `sudo`;
- de code in `/opt/olkeharnass`, data (database, logboek) in `/opt/olkeharnass/data`;
- een container `olkeharnass-socket-proxy` op `127.0.0.1:2375`, die alleen containers
  en logs laat lezen en containers laat herstarten;
- een systemd-dienst `olkeharnass` op `127.0.0.1:8400`.

Ollama en het model `qwen3:4b-instruct-2507-q4_K_M` staan er al.

## 0. Controle vooraf

```bash
python3 --version                                  # 3.11 of nieuwer (Debian 13: 3.13)
python3 -c "import venv, ensurepip" && echo venv-ok  # anders: sudo apt install python3-venv
ollama list | grep qwen3:4b-instruct-2507-q4_K_M
ss -ltn | grep -E ':(2375|8400) ' || echo poorten-vrij
```

## 1. Gebruiker

```bash
sudo useradd --system --home-dir /opt/olkeharnass/data --no-create-home \
  --shell /usr/sbin/nologin olkeharnass
```

## 2. Code en Python-omgeving

```bash
sudo mkdir -p /opt/olkeharnass && sudo chown auke:auke /opt/olkeharnass
git clone https://github.com/ajaxvaart/olkeharnass /opt/olkeharnass
cd /opt/olkeharnass
python3 -m venv .venv && .venv/bin/pip install -e .
mkdir -p data && sudo chown olkeharnass:olkeharnass data && sudo chmod 750 data
```

De code blijft van `auke` (bijwerken met `git pull`); de dienst mag hem alleen lezen.
Alleen `data/` is van de dienst.

## 3. Config

```bash
cp config/olkeharnass.example.yaml config/olkeharnass.yaml
.venv/bin/olkeharnass-key nieuw bord     # zet de hash bij apps.bord.key_sha256
nano config/olkeharnass.yaml
```

Aanpassen in `config/olkeharnass.yaml`:

- `apps`: haal `pa` en `dashboard` weg zolang die apps er niet zijn; laat `bord` staan.
- `models`: `cloud-snel` en `cloud-sterk` mogen weg (alles lokaal).
- `assistent.cloudflare`: `team_domein` en `aud` uit Cloudflare (stap 8), en je e-mailadres.
  Tot je die hebt: laat het blok staan met de voorbeeldwaarden. Chatten werkt dan
  thuis al, toestaan nog nergens.

## 4. API-keys van de apps

De keys staan in de configbestanden van de apps. Ze gaan naar `/opt/olkeharnass/.env`,
eigendom van root, alleen leesbaar voor root (systemd leest het bij het starten).

```bash
sudo install -m 600 -o root -g root /dev/null /opt/olkeharnass/.env
{
  echo "SONARR_API_KEY=$(grep -oP '(?<=<ApiKey>)[^<]+' /srv/docker/sonarr/config.xml)"
  echo "RADARR_API_KEY=$(grep -oP '(?<=<ApiKey>)[^<]+' /srv/docker/radarr/config.xml)"
  echo "PROWLARR_API_KEY=$(grep -oP '(?<=<ApiKey>)[^<]+' /srv/docker/prowlarr/config.xml)"
  echo "SABNZBD_API_KEY=$(grep -oP '^api_key = \K.+' /srv/docker/sabnzbd/sabnzbd.ini)"
} | sudo tee /opt/olkeharnass/.env > /dev/null
sudo grep -c '=.' /opt/olkeharnass/.env     # verwacht: 4
```

Gebruik je een ntfy-token voor het bord, voeg dan ook `NTFY_TOKEN=...` toe en zet
`token_env: NTFY_TOKEN` onder `assistent.ntfy`.

## 5. Socket-proxy

```bash
docker compose -f /opt/olkeharnass/deploy/socket-proxy/compose.yml up -d
curl -s http://127.0.0.1:2375/containers/json | head -c 150; echo     # lijst met containers
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:2375/containers/create  # 403
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:2375/containers/sonarr/exec  # 403
```

## 6. Dienst

```bash
sudo cp /opt/olkeharnass/deploy/olkeharnass.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now olkeharnass
systemctl status olkeharnass --no-pager
curl -s http://127.0.0.1:8400/health
```

Bij een fout: `sudo journalctl -u olkeharnass -n 50 --no-pager` (auke zit niet in de groep systemd-journal). Het logboek lezen: `sudo tail -n 5 /opt/olkeharnass/data/acties.jsonl` (data/ is alleen van de dienst).

## 7. Proef vanaf de server

```bash
curl -s -X POST http://127.0.0.1:8400/api/assistent/send \
  -H 'X-Bord-Assistent: 1' -H 'Content-Type: application/json' \
  -d '{"text": "Hoe staat de server ervoor?"}'
ollama ps          # het model hoort op 100% GPU te staan
```

De eerste vraag duurt langer (model laden, ongeveer 10 tot 20 seconden). Vraag daarna
bijvoorbeeld "herstart radarr" en kijk of er een voorstel terugkomt; uitvoeren kan hier
niet (geen Cloudflare-token), dat is goed.

## 8. Cloudflare (Olke)

In Cloudflare Zero Trust, bij Access > Applications, de applicatie van het bord:

- **AUD tag**: op het tabblad Overview, "Application Audience (AUD) Tag".
- **team_domein**: Settings > Custom Pages (of de login-URL), `<team>.cloudflareaccess.com`.

Zet beide in `config/olkeharnass.yaml` en herstart: `sudo systemctl restart olkeharnass`.

Daarna de bord-kant (Caddy-route en `assistent.js`) uit de PR in OlkeDashboard uitrollen.

## 9. Systeemacties (Ollama en server herstarten)

Optioneel. De dienst draait met `NoNewPrivileges`, dus sudo kan niet. In plaats daarvan
krijgt de gebruiker `olkeharnass` via polkit precies twee rechten: `ollama.service`
herstarten en de server herstarten.

```bash
dpkg -s polkitd > /dev/null && echo polkit-ok     # anders: sudo apt install -y polkitd
sudo install -m 644 /opt/olkeharnass/deploy/polkit/50-olkeharnass.rules /etc/polkit-1/rules.d/
```

Zet `updates`, `herstart_ollama` en `herstart_server` bij `apps.bord.tools` in de config en
herstart de dienst. Een herstart van de server gebeurt een minuut na Toestaan; afbreken
kan met `sudo shutdown -c`.

Terugdraaien: `sudo rm /etc/polkit-1/rules.d/50-olkeharnass.rules`.

## 10. Onderzoek van alle diensten

De knop Onderzoek en de tool `onderzoek` lezen ook Bazarr, Seerr, Jellyfin, Immich en
Vaultwarden. Daarvoor vier keys erbij in `.env` (Vaultwarden heeft er geen nodig):

```bash
{
  echo "BAZARR_API_KEY=$(awk '/^auth:/{a=1;next} a&&/apikey:/{gsub(/["'"'"']/,"",$2);print $2;exit}' /srv/docker/bazarr/config/config.yaml)"
  echo "SEERR_API_KEY=$(python3 -c "import json;print(json.load(open('/srv/docker/seerr/settings.json'))['main']['apiKey'])")"
  grep -E '^(JELLYFIN|IMMICH)_API_KEY=' ~/.config/bord/secrets.env
} | sudo tee -a /opt/olkeharnass/.env > /dev/null
sudo grep -c '=.' /opt/olkeharnass/.env     # verwacht: 8
```

Neem uit `config/olkeharnass.example.yaml` over: de nieuwe `diensten`, de langere lijsten
`containers_logs` en `containers_herstarten`, en `onderzoek` en `bazarr_zoek_ondertitels`
bij `apps.bord.tools`. Daarna `sudo systemctl restart olkeharnass`.

Terugdraaien: `sudo sed -i '/^\(BAZARR\|SEERR\|JELLYFIN\|IMMICH\)_API_KEY=/d' /opt/olkeharnass/.env`
en de config terugzetten.

## Terugdraaien

Elke stap los, in omgekeerde volgorde:

```bash
sudo systemctl disable --now olkeharnass && sudo rm /etc/systemd/system/olkeharnass.service \
  && sudo systemctl daemon-reload                                          # 6
docker compose -f /opt/olkeharnass/deploy/socket-proxy/compose.yml down   # 5
sudo rm -rf /opt/olkeharnass                                               # 2 t/m 4
sudo userdel olkeharnass                                                   # 1
```

Alleen de assistent uitzetten, de rest laten draaien: haal het blok `assistent` uit de
config en herstart. Acties tijdelijk onmogelijk maken: haal het blok `cloudflare` weg.

## Wat de assistent wel en niet kan

- Lezen: bord-status, containers, logs (van de containers in `containers_logs`, met
  geheimen gemaskeerd), Prometheus, Sonarr/Radarr/Prowlarr-meldingen en wachtrijen, SABnzbd.
- Onderzoek per dienst (knop in het bord of tool `onderzoek`): feiten verzamelen en bekende
  problemen herkennen in code (`src/olkeharnass/onderzoek/`), zonder model; bekende
  oplossingen worden voorstellen.
- Voorstellen (pas na Toestaan in het bord, via Cloudflare Access): container herstarten
  (alleen die in `containers_herstarten`), ontbrekende items zoeken, indexers testen,
  SABnzbd pauzeren/hervatten, mislukte download opnieuw.
- Systeem: updates bekijken (lezen), Ollama herstarten (middel) en de server herstarten
  (hoog, een minuut na toestemming). Dat mag via de polkit-regel hieronder, niet via sudo.
- Niet: shell, sudo, bestanden, Docker exec, apt install/upgrade, iets buiten deze lijst.
- Elk besluit (voorgesteld, toegestaan, geweigerd, mislukt, toestaan zonder Access)
  komt in `data/acties.jsonl`; de besluiten geven ook een ntfy-melding.
