# Shared setup scripts

Shared setup scripts are optional and opt-in.

Path split:

- `/usr/share/kuhbs/setup-scripts/kuhbs/` - scripts shipped by KUHBS
- `~/.kuhbs/setup-scripts/` - user-owned scripts

A kuhb can list shared setup scripts in `kuhb.yml`, for example:

```yaml
kuhs:

  tpl:
    setup_scripts:
      - /usr/share/kuhbs/setup-scripts/kuhbs/setup/example-kuhbs-script.sh
      - ~/.kuhbs/setup-scripts/example-user-script.sh
```

Per-kuhb scripts under `my-kuhbs/<id>/scripts/<type>/` are discovered automatically and do not need to be listed. Use local numeric prefixes like `10-packages.sh` and `20-app-setup.sh` when more than one script needs an order.

The concatenator runs explicit `setup_scripts` entries first, in the order they appear in `kuhb.yml`. Per-kuhb script directories are appended after that and expanded in stable filename order.
