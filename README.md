# GBA Link Play

Play GBA link multiplayer through RomM in two browsers. Both mGBA instances
run on the server. Requires Linux and a RomM build with `per_player_saves` support.

```sh
export ROMM_LIBRARY=/absolute/path/to/romm/library
export GBA_LINK_BROKER_SECRET='a-long-random-shared-secret'
docker compose up --build -d
```

For Caddy on the host or using host networking, with RomM on port 3000:

```caddyfile
romm.example.com {
    redir /gba-link /gba-link/ 308
    handle /gba-link/* {
        reverse_proxy 127.0.0.1:8080
    }
    handle {
        reverse_proxy 127.0.0.1:3000
    }
}
```

Add this to RomM's `config.yml`, using the same broker secret:

```yaml
streaming:
  enabled: true
  containers:
    - host: https://romm.example.com/gba-link/
      broker_host: https://romm.example.com
      broker_secret: replace-with-the-same-GBA_LINK_BROKER_SECRET
      protocol: webstation
      per_player_saves: true
      multiplayer: true
      capabilities: [join]
      clears_stale_saves: true
      subfolder: /gba-link
      library_path: /roms
      platforms:
        gba:
          emulator: mgba-link
          label: GBA Link Play
```

If `STREAMING_BROKER_SECRET` is set in RomM, use that value for both services.

Internet play needs WebRTC UDP connectivity in addition to Caddy. Set your
STUN/TURN configuration before starting the service:

```sh
export GBA_LINK_ICE_SERVERS='[{"urls":"stun:turn.example:3478"},{"urls":"turn:turn.example:3478","username":"gba-link","credential":"replace-me"}]'
```

Open an uncompressed `.gba` ROM in RomM and choose GBA Link Play. The second
player joins through RomM. The game starts when both browsers are connected
and pauses if either player disconnects.

Save in-game before ending the room. Each player's save is stored separately.
To import a save, upload a ZIP containing `game.sav` and tag it `mgba-link`.

Each service runs one room for two players using the same ROM. Save states
are not supported.
