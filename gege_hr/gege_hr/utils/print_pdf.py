"""Trả PDF qua print pipeline chuẩn của Frappe sau khi API đã tự kiểm quyền.

``frappe.utils.print_format.download_pdf`` luôn gọi ``validate_print_permission``
(quyền read/print theo DocPerm + User Permission). Các API của gege_hr đã kiểm
"HR hoặc đúng chủ bản ghi" ở tầng app, và nhân viên cố ý KHÔNG có quyền đọc
Salary Slip qua desk/REST (phiếu chưa phát hành không được lộ) → dùng
``frappe.flags.ignore_print_permissions`` (cờ chuẩn mà ``get_print`` tôn trọng)
và tự điền ``frappe.response`` giống ``download_pdf``.
"""

from __future__ import annotations


def send_pdf(doctype: str, name: str, print_format: str | None = None) -> None:
    import frappe

    doc = frappe.get_doc(doctype, name)
    prev = frappe.flags.ignore_print_permissions
    frappe.flags.ignore_print_permissions = True
    try:
        pdf_file = frappe.get_print(doctype, name, print_format, doc=doc, as_pdf=True)
    finally:
        frappe.flags.ignore_print_permissions = prev
    frappe.local.response.filename = "{name}.pdf".format(name=name.replace(" ", "-").replace("/", "-"))
    frappe.local.response.filecontent = pdf_file
    frappe.local.response.type = "pdf"
