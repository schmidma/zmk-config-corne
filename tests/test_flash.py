import contextlib
import errno
import importlib.machinery
import importlib.util
import io
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "flash"
loader = importlib.machinery.SourceFileLoader("flash", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
flash = importlib.util.module_from_spec(spec)
loader.exec_module(flash)


def uf2(address=flash.START, family=flash.FAMILY, flags=0x2000, number=0, count=1):
    block = bytearray(512)
    struct.pack_into(
        "<8I",
        block,
        0,
        0x0A324655,
        0x9E5D5157,
        flags,
        address,
        256,
        number,
        count,
        family,
    )
    struct.pack_into("<I", block, 508, 0x0AB16F30)
    return bytes(block)


class UF2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "test.uf2"

    def check(self, data):
        self.file.write_bytes(data)
        return flash.load_firmware(self.file)

    def test_invalid_blocks(self):
        good = uf2()
        cases = [
            good[:-1],
            bytes(512),
            uf2(family=1),
            uf2(flags=0x2001),
            uf2(address=0),
            uf2(address=flash.END),
            uf2(address=flash.START + 1),
            uf2(count=2),
            uf2(count=2) * 2,
            uf2(count=2) + uf2(address=flash.START, number=1, count=2),
        ]
        for data in cases:
            with self.subTest(data=data[:32]), self.assertRaises(flash.FlashError):
                self.check(data)
        self.assertEqual(self.check(good), good)
        self.assertEqual(
            len(
                self.check(
                    uf2(count=2) + uf2(address=flash.START + 256, number=1, count=2)
                )
            ),
            1024,
        )

    def test_count_and_both_files_before_device(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            flash.main([str(self.file)])
        left = self.file
        left.write_bytes(uf2())
        with (
            mock.patch.object(
                flash, "volumes", side_effect=AssertionError("device touched")
            ),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(flash.FlashError),
        ):
            flash.run(left, self.file.parent / "absent.uf2")


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mount = Path(self.tmp.name)
        mounted = mock.patch.object(flash.os.path, "ismount", return_value=True)
        mounted.start()
        self.addCleanup(mounted.stop)
        (self.mount / "INFO_UF2.TXT").write_text(
            "Board-ID: nRF52840-nicenano-v2\nModel: nice!nano\n"
        )
        self.volume = {"path": "/dev/sdb1", "mount": str(self.mount), "serial": "left"}
        self.sysfs = self.mount / "sysfs"
        self.sysfs.mkdir()
        sysfs = mock.patch.object(flash, "USB_DEVICES", self.sysfs)
        sysfs.start()
        self.addCleanup(sysfs.stop)

    def app(
        self,
        serial="left",
        vendor="1d50",
        pid="615e",
        product="Corne",
        manufacturer="ZMK Project",
    ):
        device = self.sysfs / "1-1"
        device.mkdir(exist_ok=True)
        for name, value in (
            ("serial", serial),
            ("idVendor", vendor),
            ("idProduct", pid),
            ("product", product),
            ("manufacturer", manufacturer),
        ):
            (device / name).write_text(value)
        return device

    def firmware(self):
        left = self.mount / "left.uf2"
        right = self.mount / "right.uf2"
        left.write_bytes(uf2())
        right.write_bytes(uf2(address=flash.START + 256))
        return left, right

    def test_nested_usb_filter_and_multiple(self):
        drives = {
            "blockdevices": [
                {
                    "path": "/dev/sda",
                    "tran": "sata",
                    "children": [
                        {
                            "path": "/dev/sda1",
                            "label": "NICENANO",
                            "fstype": "vfat",
                            "mountpoints": ["/bad"],
                        }
                    ],
                },
                {
                    "path": "/dev/sdb",
                    "tran": "usb",
                    "serial": "abc",
                    "children": [
                        {
                            "path": "/dev/sdb1",
                            "label": "NICENANO",
                            "fstype": "vfat",
                            "mountpoints": [str(self.mount)],
                        }
                    ],
                },
                {
                    "path": "/dev/sdc",
                    "tran": "usb",
                    "children": [
                        {"path": "/dev/sdc1", "label": "OTHER", "fstype": "vfat"}
                    ],
                },
            ]
        }
        with mock.patch.object(
            flash.subprocess, "run", return_value=mock.Mock(stdout=json.dumps(drives))
        ) as command:
            self.assertEqual(flash.volumes(), [dict(self.volume, serial="abc")])
            self.assertIn("--tree", command.call_args.args[0])
            drives["blockdevices"].append(
                {
                    "path": "/dev/sdd",
                    "tran": "usb",
                    "label": "NICENANO",
                    "fstype": "vfat",
                }
            )
            with (
                mock.patch.object(
                    flash,
                    "volumes",
                    side_effect=[
                        [self.volume, dict(self.volume, path="/dev/sdd")],
                        [self.volume],
                    ],
                ),
                mock.patch.object(flash.time, "sleep"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(flash.wait_for_drive(), self.volume)

    def test_identity_and_mount_failure(self):
        flash.identity(self.volume)
        (self.mount / "INFO_UF2.TXT").write_text("Board-ID: other\nModel: nice!nano\n")
        with self.assertRaisesRegex(flash.FlashError, "Board-ID"):
            flash.identity(self.volume)
        unmounted = dict(self.volume, mount=None)
        with (
            mock.patch.object(flash, "volumes", return_value=[unmounted]),
            mock.patch.object(flash.shutil, "which", return_value="/usr/bin/udisksctl"),
            mock.patch.object(
                flash.subprocess,
                "run",
                return_value=mock.Mock(
                    returncode=1, stdout="", stderr="Not authorized to mount /dev/sdb1"
                ),
            ),
            mock.patch.object(flash.time, "monotonic", side_effect=[0, 3]),
            self.assertRaisesRegex(
                flash.FlashError, "Not authorized to mount /dev/sdb1"
            ),
        ):
            flash.wait_for_drive()

    def test_automatic_mount_rescans_drive(self):
        unmounted = dict(self.volume, mount=None)
        with (
            mock.patch.object(
                flash, "volumes", side_effect=[[unmounted], [self.volume]]
            ) as scan,
            mock.patch.object(flash.shutil, "which", return_value="/usr/bin/udisksctl"),
            mock.patch.object(
                flash.subprocess,
                "run",
                return_value=mock.Mock(returncode=0, stdout="localized output"),
            ) as command,
            mock.patch.object(flash.time, "sleep"),
        ):
            self.assertEqual(flash.wait_for_drive(), self.volume)
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(
            command.call_args.args[0],
            [
                "udisksctl",
                "mount",
                "--block-device",
                "/dev/sdb1",
                "--no-user-interaction",
            ],
        )

    def test_mount_race_waits_for_verified_mount(self):
        unmounted = dict(self.volume, mount=None)
        for returncode in (0, 1):
            with (
                self.subTest(returncode=returncode),
                mock.patch.object(
                    flash,
                    "volumes",
                    side_effect=[[unmounted], [unmounted], [self.volume]],
                ),
                mock.patch.object(
                    flash.shutil, "which", return_value="/usr/bin/udisksctl"
                ),
                mock.patch.object(
                    flash.subprocess,
                    "run",
                    return_value=mock.Mock(
                        returncode=returncode, stdout="", stderr="Already mounted"
                    ),
                ) as command,
                mock.patch.object(flash.time, "monotonic", side_effect=[0, 1]),
                mock.patch.object(flash.time, "sleep") as pause,
            ):
                self.assertEqual(flash.wait_for_drive(), self.volume)
            command.assert_called_once()
            pause.assert_called_once_with(flash.POLL)

    def test_mount_settle_rejects_changed_or_ambiguous_drive(self):
        unmounted = dict(self.volume, mount=None)
        for current in (
            [],
            [dict(self.volume, serial="other")],
            [dict(self.volume, path="/dev/sdc1")],
            [self.volume, dict(self.volume, path="/dev/sdc1")],
        ):
            with (
                self.subTest(current=current),
                mock.patch.object(flash, "volumes", side_effect=[[unmounted], current]),
                mock.patch.object(
                    flash.shutil, "which", return_value="/usr/bin/udisksctl"
                ),
                mock.patch.object(
                    flash.subprocess, "run", return_value=mock.Mock(returncode=1)
                ),
                self.assertRaisesRegex(flash.FlashError, "while mounting"),
            ):
                flash.wait_for_drive()

    def test_successful_mount_command_still_requires_verified_mount(self):
        unmounted = dict(self.volume, mount=None)
        with (
            mock.patch.object(flash, "volumes", return_value=[unmounted]),
            mock.patch.object(flash.shutil, "which", return_value="/usr/bin/udisksctl"),
            mock.patch.object(
                flash.subprocess,
                "run",
                return_value=mock.Mock(returncode=0, stdout="", stderr=""),
            ) as command,
            mock.patch.object(flash.time, "monotonic", side_effect=[0, 3]),
            self.assertRaisesRegex(flash.FlashError, "still unmounted"),
        ):
            flash.wait_for_drive()
        command.assert_called_once()

    def test_mount_race_rechecks_bootloader_identity(self):
        (self.mount / "INFO_UF2.TXT").write_text("Board-ID: other\n")
        with (
            mock.patch.object(
                flash,
                "volumes",
                side_effect=[[dict(self.volume, mount=None)], [self.volume]],
            ),
            mock.patch.object(flash.shutil, "which", return_value="/usr/bin/udisksctl"),
            mock.patch.object(
                flash.subprocess, "run", return_value=mock.Mock(returncode=1)
            ),
            self.assertRaisesRegex(flash.FlashError, "Board-ID"),
        ):
            flash.wait_for_drive()

    def test_same_device_waits_for_other_half(self):
        other = dict(self.volume, serial="right")
        with (
            mock.patch.object(flash, "volumes", side_effect=[[self.volume], [other]]),
            mock.patch.object(flash.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(flash.wait_for_drive("left"), other)
        self.assertIn("connect the other physical half", output.getvalue())

    def test_recheck_prevents_write(self):
        with (
            mock.patch.object(flash, "volumes", return_value=[]),
            self.assertRaises(flash.FlashError),
        ):
            flash.transfer(self.volume, uf2())
        self.assertFalse((self.mount / "firmware.uf2").exists())

    def test_flow_retry_right_without_repeating_left(self):
        left = self.mount / "left.uf2"
        right = self.mount / "right.uf2"
        left.write_bytes(uf2())
        right.write_bytes(uf2(address=flash.START + 256))
        other = dict(self.volume, path="/dev/sdc1", serial="right")
        with (
            mock.patch.object(
                flash, "wait_for_drive", side_effect=[self.volume, other, other]
            ) as wait,
            mock.patch.object(
                flash,
                "transfer",
                side_effect=[None, flash.FlashError("uncertain"), None],
            ) as transfer,
            mock.patch.object(flash, "wait_for_disappearance") as gone,
            mock.patch.object(flash, "retry_or_quit", return_value=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(flash.run(left, right), 0)
        self.assertEqual(
            [c.args[0]["path"] for c in transfer.call_args_list],
            ["/dev/sdb1", "/dev/sdc1", "/dev/sdc1"],
        )
        self.assertEqual(gone.call_count, 2)
        self.assertEqual(wait.call_count, 3)
        self.assertEqual(wait.call_args_list[1].args, ("left",))

    def test_full_flow_simulated_mounts(self):
        with tempfile.TemporaryDirectory() as right_dir:
            right_mount = Path(right_dir)
            (right_mount / "INFO_UF2.TXT").write_text("Board-ID: nRF52840-nicenano\n")
            right_volume = {
                "path": "/dev/sdc1",
                "mount": str(right_mount),
                "serial": "right",
            }
            left = self.mount / "left.uf2"
            right = self.mount / "right.uf2"
            left.write_bytes(uf2())
            right.write_bytes(uf2(address=flash.START + 256))
            # Each half: discovery, pre-write revalidation, disappearance.
            scans = [
                [self.volume],
                [self.volume],
                [],
                [right_volume],
                [right_volume],
                [],
            ]
            with (
                mock.patch.object(flash, "volumes", side_effect=scans),
                mock.patch.object(flash.time, "sleep"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(flash.run(left, right), 0)
            self.assertEqual(
                (self.mount / "firmware.uf2").read_bytes(), left.read_bytes()
            )
            self.assertEqual(
                (right_mount / "firmware.uf2").read_bytes(), right.read_bytes()
            )

    def test_transfer_error_never_reports_success(self):
        with (
            mock.patch.object(flash, "volumes", return_value=[self.volume]),
            mock.patch.object(
                flash.os, "fsync", side_effect=OSError("device rebooted")
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(flash.FlashError, "completion uncertain"),
        ):
            flash.transfer(self.volume, uf2())

    def test_late_eio_same_serial_return_advances_without_retry(self):
        left, right = self.firmware()
        right_mount = self.mount / "right-mount"
        right_mount.mkdir()
        (right_mount / "INFO_UF2.TXT").write_text("Board-ID: nRF52840-nicenano-v2\n")
        other = dict(
            self.volume, path="/dev/sdc1", mount=str(right_mount), serial="right"
        )

        def fsync(fd):
            if fsync_calls.call_count == 1:
                self.app()
                raise OSError(errno.EIO, "rebooted")

        with (
            mock.patch.object(
                flash, "wait_for_drive", side_effect=[self.volume, other]
            ),
            mock.patch.object(flash, "volumes", side_effect=[[self.volume], [other]]),
            mock.patch.object(flash, "wait_for_disappearance") as gone,
            mock.patch.object(flash.os, "fsync", side_effect=fsync) as fsync_calls,
            mock.patch.object(flash, "retry_or_quit") as retry,
            mock.patch.object(flash, "confirm_or_retry") as confirm,
            mock.patch.object(
                flash.time, "sleep", side_effect=AssertionError("unexpected polling")
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(flash.run(left, right), 0)
        self.assertIn("[OK] LEFT restarted.", output.getvalue())
        self.assertNotIn("sync interrupted", output.getvalue())
        self.assertEqual(fsync_calls.call_count, 2)
        self.assertEqual(gone.call_count, 2)
        retry.assert_not_called()
        confirm.assert_not_called()

    def test_partial_writes_and_write_error_are_hard_failures(self):
        self.app()
        data = uf2()
        real_write = os.write
        calls = 0

        def write(fd, chunk):
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, chunk[:100])
            raise OSError(errno.EIO, "device gone")

        with (
            mock.patch.object(flash, "volumes", return_value=[self.volume]),
            mock.patch.object(flash.os, "write", side_effect=write),
            mock.patch.object(flash.os, "fsync") as sync,
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(flash.FlashError) as raised,
        ):
            flash.transfer(self.volume, data)
        self.assertNotIsInstance(raised.exception, flash.LateSyncError)
        sync.assert_not_called()
        self.assertEqual((self.mount / "firmware.uf2").read_bytes(), data[:100])

    def test_successful_partial_writes_preserve_offsets(self):
        data = uf2()
        real_write = os.write
        chunks = []

        def write(fd, chunk):
            chunks.append(bytes(chunk))
            return real_write(fd, chunk[:100] if len(chunks) == 1 else chunk)

        with (
            mock.patch.object(flash, "volumes", return_value=[self.volume]),
            mock.patch.object(flash.os, "write", side_effect=write),
            mock.patch.object(flash.os, "fsync") as sync,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            flash.transfer(self.volume, data)
        self.assertEqual(chunks, [data, data[100:]])
        self.assertEqual((self.mount / "firmware.uf2").read_bytes(), data)
        sync.assert_called_once()

    def test_write_error_is_not_masked_by_close_error(self):
        real_close = os.close

        def close(fd):
            real_close(fd)
            raise OSError(errno.ENODEV, "close lost device")

        with (
            mock.patch.object(flash, "volumes", return_value=[self.volume]),
            mock.patch.object(
                flash.os,
                "write",
                side_effect=OSError(errno.EIO, "payload write failed"),
            ),
            mock.patch.object(flash.os, "close", side_effect=close),
            mock.patch.object(flash.os, "fsync") as sync,
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(flash.FlashError, "payload write failed") as raised,
        ):
            flash.transfer(self.volume, uf2())
        self.assertNotIsInstance(raised.exception, flash.LateSyncError)
        sync.assert_not_called()

    def test_early_error_with_matching_app_does_not_advance(self):
        left, right = self.firmware()
        self.app()
        with (
            mock.patch.object(flash, "wait_for_drive", return_value=self.volume),
            mock.patch.object(flash, "volumes", return_value=[self.volume]),
            mock.patch.object(
                flash.os, "write", side_effect=OSError(errno.EIO, "write failed")
            ),
            mock.patch.object(flash, "wait_for_disappearance") as gone,
            mock.patch.object(flash, "wait_for_app") as returned,
            mock.patch.object(flash, "retry_or_quit", return_value=False),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(flash.run(left, right), 1)
        gone.assert_not_called()
        returned.assert_not_called()

    def test_late_error_bootloader_still_present_never_advances(self):
        left, right = self.firmware()
        self.app()
        with (
            mock.patch.object(flash, "wait_for_drive", return_value=self.volume),
            mock.patch.object(
                flash, "transfer", side_effect=flash.LateSyncError("EIO")
            ),
            mock.patch.object(
                flash,
                "wait_for_disappearance",
                side_effect=flash.FlashError("still present"),
            ),
            mock.patch.object(flash, "wait_for_app") as returned,
            mock.patch.object(flash, "confirm_or_retry") as confirm,
            mock.patch.object(flash, "retry_or_quit", return_value=False),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(flash.run(left, right), 1)
        returned.assert_not_called()
        confirm.assert_not_called()

    def test_zero_write_is_failure(self):
        with (
            mock.patch.object(flash, "volumes", return_value=[self.volume]),
            mock.patch.object(flash.os, "write", return_value=0),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(flash.FlashError) as raised,
        ):
            flash.transfer(self.volume, uf2())
        self.assertNotIsInstance(raised.exception, flash.LateSyncError)

    def test_late_sync_and_close_error_classification(self):
        for operation in ("fsync", "close"):
            for code, late in ((errno.EIO, True), (errno.ENOSPC, False)):
                with self.subTest(operation=operation, code=code):
                    target = self.mount / "firmware.uf2"
                    target.unlink(missing_ok=True)
                    real_close = os.close

                    def close(fd, real_close=real_close, code=code):
                        real_close(fd)
                        raise OSError(code, "close failed")

                    patch = mock.patch.object(
                        flash.os,
                        operation,
                        side_effect=(
                            OSError(code, "sync failed")
                            if operation == "fsync"
                            else close
                        ),
                    )
                    with (
                        mock.patch.object(flash, "volumes", return_value=[self.volume]),
                        patch,
                        contextlib.redirect_stdout(io.StringIO()),
                        self.assertRaises(flash.FlashError) as raised,
                    ):
                        flash.transfer(self.volume, uf2())
                    self.assertEqual(
                        isinstance(raised.exception, flash.LateSyncError), late
                    )

    def test_app_match_requires_new_exact_identity(self):
        self.assertFalse(flash.wait_for_app("", set()))
        self.assertFalse(flash.wait_for_app("left", {"left"}))
        self.assertFalse(flash.wait_for_app("left", None))
        for change in (
            {"serial": "other"},
            {"serial": ""},
            {"vendor": "239a"},
            {"pid": "00b3"},
            {"product": "Other"},
            {"manufacturer": "Other"},
        ):
            with self.subTest(change=change):
                self.app(**change)
                with (
                    mock.patch.object(flash.time, "monotonic", side_effect=[0, 5]),
                    mock.patch.object(flash.time, "sleep"),
                ):
                    self.assertFalse(flash.wait_for_app("left", set()))
        self.app()
        self.assertTrue(flash.wait_for_app("left", set()))
        with mock.patch.object(flash, "USB_DEVICES", self.mount / "absent"):
            self.assertIsNone(flash.app_devices())
            self.assertFalse(flash.wait_for_app("left", flash.app_devices()))
        with (
            mock.patch.object(flash, "app_devices", side_effect=[set(), {"left"}]),
            mock.patch.object(flash.time, "monotonic", side_effect=[0, 1, 2]),
            mock.patch.object(flash.time, "sleep"),
        ):
            self.assertTrue(flash.wait_for_app("left", set()))

    def test_sysfs_unavailable_after_valid_baseline(self):
        with (
            mock.patch.object(flash, "app_devices", side_effect=[None, {"left"}]),
            mock.patch.object(flash.time, "monotonic", side_effect=[0, 1]),
            mock.patch.object(flash.time, "sleep"),
        ):
            self.assertTrue(flash.wait_for_app("left", set()))
        with (
            mock.patch.object(flash, "app_devices", return_value=None),
            mock.patch.object(flash.time, "monotonic", side_effect=[0, 5]),
            mock.patch.object(flash.time, "sleep"),
        ):
            self.assertFalse(flash.wait_for_app("left", set()))

    def test_right_uncertain_manual_choices(self):
        left, right = self.firmware()
        other = dict(self.volume, path="/dev/sdc1", serial="right")
        for choices, expected, count in (
            (["", "y"], 0, 2),
            (["r", "y"], 0, 3),
            (["q"], 1, 2),
        ):
            with self.subTest(choices=choices):
                volumes = [self.volume] + [other] * (count - 1)

                def transfer(volume, data):
                    if volume["serial"] == "right":
                        raise flash.LateSyncError("EIO")

                with (
                    mock.patch.object(flash, "wait_for_drive", side_effect=volumes),
                    mock.patch.object(flash, "transfer", side_effect=transfer),
                    mock.patch.object(flash, "wait_for_disappearance"),
                    mock.patch.object(flash, "wait_for_app", return_value=False),
                    mock.patch.object(flash.sys.stdin, "isatty", return_value=True),
                    mock.patch("builtins.input", side_effect=choices) as prompt,
                    contextlib.redirect_stdout(io.StringIO()) as output,
                    contextlib.redirect_stderr(io.StringIO()) as notices,
                ):
                    self.assertEqual(flash.run(left, right), expected)
                self.assertEqual(
                    output.getvalue().count("RIGHT works (confirmed by you)"),
                    int("y" in choices),
                )
                self.assertIn("RIGHT disconnected after transfer.", notices.getvalue())
                self.assertIn(
                    "Its reboot cannot be verified over USB.", notices.getvalue()
                )
                self.assertNotIn("EIO", notices.getvalue())
                self.assertEqual(
                    prompt.call_args.args[0],
                    "Does the right half work? [y] Yes / [r] Retry / [q] Quit: ",
                )
                if "y" in choices:
                    self.assertIn("Done. Test both halves together.", output.getvalue())
        for interactive, answers in ((True, [EOFError]), (False, [])):
            with (
                mock.patch.object(flash.sys.stdin, "isatty", return_value=interactive),
                mock.patch("builtins.input", side_effect=answers) as prompt,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(flash.confirm_or_retry("RIGHT"), "q")
            if not interactive:
                prompt.assert_not_called()

    def test_no_advance_without_disappearance(self):
        with (
            mock.patch.object(flash, "volumes", return_value=[self.volume]),
            mock.patch.object(flash.time, "monotonic", side_effect=[0, 19, 21]),
            mock.patch.object(flash.time, "sleep"),
            self.assertRaisesRegex(flash.FlashError, "did not disappear"),
        ):
            flash.wait_for_disappearance(self.volume["path"])

    def test_disappearance_and_interrupt(self):
        with (
            mock.patch.object(flash, "volumes", side_effect=[[self.volume], []]),
            mock.patch.object(flash.time, "sleep"),
        ):
            flash.wait_for_disappearance(self.volume["path"])
        with (
            mock.patch.object(flash, "run", side_effect=KeyboardInterrupt),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(flash.main(["left.uf2", "right.uf2"]), 130)


if __name__ == "__main__":
    unittest.main()
