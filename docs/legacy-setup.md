# Legacy setup (pre-generated credentials)

> This is the original setup flow, kept for back-compat. Most people should use
> the **[self-serve on-device setup](../README.md#setup)** instead — it needs no
> client secret and no `generate_token.sh`, and end users never touch a cable.
>
> This path is still useful if you want to control a fixed Spotify account from a
> device you don't want to run the on-device OAuth on. It boots straight into the
> app, bypassing the setup screens, whenever `SPOTIFY_CREDENTIALS` is present in
> your secrets.

There's also a [YouTube demo/tutorial](https://youtu.be/iOz5XUVkFkY) covering this
original flow.

## How it differs

The legacy flow stores a **full pre-generated credentials dict** (refresh token,
client id, client secret, device id) in your secrets, generated on your computer
with a helper script. When `SPOTIFY_CREDENTIALS` is set, `launch()` skips the
self-serve setup entirely. WiFi comes from `WIFI_SSID` / `WIFI_PASSWORD` in the
same secrets file.

## 1. Install Thonny

Download and install the [Thonny IDE](https://thonny.org/), which you'll use to
connect to your Presto and upload the code.

## 2. Clone the repository

```bash
git clone https://github.com/fatihak/PrestoDeck.git
```

## 3. Create a Spotify app

- Visit [Spotify for Developers](https://developer.spotify.com/dashboard/applications) and sign in.
- Click **Create an App**, give it a name and description.
- For the Redirect URIs, enter `http://127.0.0.1:8080`.
- Tick the **Web API** box.

## 4. Generate Spotify credentials

Run the helper script to authenticate and generate your credentials (requires
`python3` on your machine):

```bash
bash adhoc/generate_token.sh
```

You'll be prompted to:

- Enter the Client ID, Client Secret, and Redirect URI for your Spotify app.
- Visit a URL to authorize your app and paste the redirected URL back.
- Select a default Spotify device to control playback from PrestoDeck.

Once complete, copy the generated `SPOTIFY_CREDENTIALS={...}` line.

## 5. Connect your Presto to your computer with a USB-C cable

## 6. Upload project files

- Open **Thonny**, set the interpreter to **MicroPython (Raspberry Pi Pico)**.
- In the Files window, right-click the root of the cloned project and choose
  **Upload to /** to copy everything to the Presto.

## 7. Store WiFi and Spotify credentials

- In Thonny, open `src/secrets.py` (copy it from `src/secrets.py.example` first).
- Set `WIFI_SSID` and `WIFI_PASSWORD`.
- Paste the `SPOTIFY_CREDENTIALS = {...}` line from step 4.

> **Alternative: keep credentials on a microSD card.** Copy
> `src/secrets.json.example` to `secrets.json`, fill in the same values, and save
> it to the **root of a FAT32-formatted microSD card** (mounted at `/sd`, so it's
> read as `/sd/secrets.json` — you don't create an `/sd` folder yourself).
> PrestoDeck reads the card first and falls back to `secrets.py` when it's
> missing — handy for swapping credentials without reflashing.

## 8. Run

- In Thonny, open `main.py` and click **Run**.
