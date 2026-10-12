# infra-ui-screenshot-guidance

# Infra UI Screenshot Guidance

Class of work: the user **sends a screenshot of a GUI they control** (domain
registrar, router admin, hosting dashboard) and asks how to configure something
— e.g. "how do I add DNS A records", "set up port forwarding". You cannot click
for them; you interpret the screenshot and give exact steps. This is a **recurring
pattern for Jonathan** (Namecheap DNS ×2, router port-forward ×1 observed in one
session) — treat every such screenshot as this class.

## Workflow
1. **Describe what you see** in the screenshot back to the user (panel name, the
   relevant control, its current state) — this confirms you're both looking at the
   same thing before you instruct.
2. **Locate the exact control**, then give tap-by-tap steps referencing the labels
   visible in *their* screenshot (not generic docs).
3. **Name the exact values** they must enter (Host, Value, port numbers, protocol).
4. **Flag the common trap** for that specific panel (below) so they don't hit it.
5. Hand back verification ("tell me when done / send another screenshot").

## Recurring GUI pitfalls (the high-value part)

### Namecheap DNS — "HOST RECORDS: manage in your cPanel account"
If the Advanced DNS page says *"You can manage host records in your cPanel
account, or transfer DNS back to Namecheap BasicDNS to manage the records here"*
with a red **"Change DNS Type"** link, the domain's nameservers point at a
third-party (cPanel) — **you cannot add A records here yet**.
- Fix: tap **Change DNS Type** → choose **Namecheap BasicDNS**. Within minutes the
  HOST RECORDS section becomes editable and shows **"ADD NEW RECORD"**.
- ⚠️ Switching DNS type drops any existing cPanel-hosted site/mail for that domain.
  Safe if the domain is a fresh homelab domain with no live site/mail — otherwise
  recreate those records after the switch. Ask first if uncertain.
- Once on BasicDNS, add: **A** record `Host=api, Value=<public IP>` and **A**
  record `Host=@, Value=<public IP>` (TTL Automatic). Repeat for each subdomain.
- **If the end goal is a Cloudflare Tunnel** (residential ISP blocks inbound 80/443,
  or CGNAT), BasicDNS is only a stepping stone. Ultimately the domain must move to
  **Cloudflare's nameservers** (dash → Add Site → Cloudflare returns 2 NS like
  `john.ns.cloudflare.com` / `lara.ns.cloudflare.com`; set those at Namecheap →
  Nameservers). Cloudflare then serves the zone and the tunnel's CNAME records.
  BasicDNS alone won't help when inbound ports are filtered — go straight to the
  Cloudflare nameserver switch in that case.

### Router port-forward — Enable checkbox vs row-select, and the Enable dropdown
Two distinct "enable" controls confuse users:
- The **checkbox in the rules table** is often **row-select** (for Delete), NOT an
  enable toggle. Don't tell the user to tick it.
- The actual enable state is the form's **"Enable" dropdown** (set to "Enable"),
  which only takes effect when the rule is **Added/Applied**. A rule shown in the
  table with the dropdown = Enable is NOT live until **Apply** is clicked and the
  table row shows it as active.
- Symptom that this is the cause: Let's Encrypt logs `Timeout during connect` to
  the public IP even though the rule "exists". Re-apply via the form (WAN, Public
  Port, IP, Private Port, Protocol TCP, **Enable dropdown = Enable**, **Apply**).
- Standard rule: Public Port `80-80` + `443-443`, IP = the LAN host (e.g.
  `192.168.1.11`), Private Port same, Protocol TCP.

### Verifying inbound reachability (phone test)
After DNS A + port-forward are set, the fastest confirmation the user can do
themselves: **turn off Wi-Fi on the phone (use cellular)** and open
`http://<subdomain>.<domain>/...`. Loads = ports are open (TLS cert follows in
~1 min via Caddy auto-ACME); times out = inbound still blocked (ISP likely filters
80/443 — pivot to Cloudflare Tunnel, see `proxmox-homelab-administration`).

## Safety / scope
- You are **advising**, not acting — these are the user's own accounts/devices.
- Before any step that could drop existing services (DNS-type switch, DMZ), ask.
- Don't store registrar/router credentials. If you need the public IP, fetch it
  from the host (`curl -4 https://ifconfig.me` on the Proxmox host) rather than
  asking the user to read it.

See `references/dns-router-steps.md` for the exact field-by-field recipes
(Namecheap BasicDNS A-record form; typical router port-forward form).
