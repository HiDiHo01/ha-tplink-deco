"""
TP-Link Deco standalone reboot endpoint tester.

Zet dit script in:
  C:\Program Files\HassWP2\config\custom_components\tplink_deco\

Run vanuit die map:
  cd "C:\Program Files\HassWP2\config\custom_components\tplink_deco"
  python deco_reboot_test.py
"""

import asyncio
import importlib
import importlib.util
import sys
import types

TPLINK_DIR = r"C:\Program Files\HassWP2\config\custom_components\tplink_deco"
HA_PACKAGES = r"C:\Program Files\HassWP2\python\Lib\site-packages"

sys.path.insert(0, HA_PACKAGES)
sys.path.insert(0, TPLINK_DIR)

HOST = "https://192.168.2.2"
USERNAME = "admin"
PASSWORD = "Supervis18!"

DRY_RUN = False  # <- Zet op False om echt te testen

CANDIDATES = [
    ("admin/device", "system", {"operation": "reboot", "params": {"mac_list": [{"mac": "00-31-92-2E-61-18"}]}},
     "device?form=system, own mac in mac_list"),
    ("admin/device", "system",    {"operation": "reboot"},
     "device?form=system, no params"),
    ("admin/device", "device",    {"operation": "reboot"},
     "device?form=device, no params"),
    ("admin/device", "operation", {"operation": "reboot"},
     "device?form=operation, no params"),
    ("admin/system", "reboot",    {"operation": "reboot"},
     "system?form=reboot, no params"),
    ("admin/system", "system",    {"operation": "reboot"},
     "system?form=system, no params"),
    ("admin/device", "system",    {"operation": "reboot", "params": {}},
     "device?form=system, empty params"),
    ("admin/device", "system",    {"operation": "reboot",
     "params": {"mac_list": []}}, "device?form=system, empty mac_list"),
]


def patch_relative_imports():
    """Make relative imports work by registering a fake parent package."""
    package_name = "tplink_deco"
    if package_name in sys.modules:
        return

    pkg = types.ModuleType(package_name)
    pkg.__path__ = [TPLINK_DIR]
    pkg.__package__ = package_name
    sys.modules[package_name] = pkg

    for module_name in ["const", "exceptions", "api"]:
        full_name = f"{package_name}.{module_name}"
        file_path = TPLINK_DIR + "\\" + module_name + ".py"
        spec = importlib.util.spec_from_file_location(
            full_name,
            file_path,
            submodule_search_locations=[],
        )
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = package_name
        sys.modules[full_name] = mod
        sys.modules[module_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            print(f"  Warning loading {module_name}: {e}")


async def main() -> None:
    print("Patching relative imports...")
    patch_relative_imports()

    import inspect

    from tplink_deco.api import TplinkDecoApi
    print("Imported TplinkDecoApi")

    # Print constructor signature so we know exactly what's expected
    sig = inspect.signature(TplinkDecoApi.__init__)
    print(f"TplinkDecoApi.__init__ signature: {sig}\n")

    import ssl

    import aiohttp

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    session = aiohttp.ClientSession(connector=connector)

    api = TplinkDecoApi(
        session=session,
        host=HOST,
        username=USERNAME,
        password=PASSWORD,
        verify_ssl=False,
    )

    print("Logging in...")
    try:
        await api.async_login_if_needed()
        print(f"Logged in, stok={api._stok}\n")
    except Exception as e:
        print(f"Login failed: {e}")
        return

    if DRY_RUN:
        print("DRY_RUN=True - geen requests verstuurd.")
        print("Zet DRY_RUN=False om echt te testen.\n")
        for _, form, _, label in CANDIDATES:
            print(f"  Would try: {label}")
        return

    print("=" * 60)
    print("Testing reboot combinations...")
    print("=" * 60 + "\n")

    for path, form, payload, label in CANDIDATES:
        print(f"--- {label} ---")
        url = f"{HOST}/cgi-bin/luci/;stok={api._stok}/{path}"
        print(f"  URL:     {url}?form={form}")
        print(f"  payload: {payload}")

        try:
            response_json = await api._async_post(
                label,
                url,
                params={"form": form},
                data=api._encode_payload(payload),
            )
            data = api._decrypt_data(label, response_json["data"])
            error_code = data.get("error_code", "?")
            print(f"  -> error_code={error_code}, data={data}")

            if error_code == 0:
                print("  SUCCESS!\n")
                print(f"Werkende combinatie: {label}")
                break
            else:
                print("  Failed\n")

        except Exception as e:
            print(f"  Exception: {e}\n")

        # Re-login tussen pogingen
        try:
            api._stok = None
            await api.async_login_if_needed()
        except Exception as e:
            print(f"Re-login failed: {e}")
            break
    await session.close()


if __name__ == "__main__":
    asyncio.run(main())
