"""Document templates for Floatex Solar — generates actual .docx files."""

from docx import Document
from docx.shared import Inches, Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
import io


# --- Styling helpers ---

def set_cell_shading(cell, color):
    shading = cell._element.get_or_add_tcPr()
    shading_elm = shading.makeelement(qn('w:shd'), {
        qn('w:val'): 'clear',
        qn('w:color'): 'auto',
        qn('w:fill'): color,
    })
    shading.append(shading_elm)


def add_header_footer(doc, project_title, doc_no):
    """Add header and footer to document."""
    section = doc.sections[0]
    section.top_margin = Cm(2.5)
    section.bottom_margin = Cm(2)
    section.left_margin = Cm(2.5)
    section.right_margin = Cm(2.5)

    # Header
    header = section.header
    hp = header.paragraphs[0]
    hp.text = f"{project_title}\n{doc_no}"
    hp.style.font.size = Pt(8)
    hp.style.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)


def styled_para(doc, text, size=11, bold=False, color=None, align=None, space_after=6):
    """Add a styled paragraph."""
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.font.size = Pt(size)
    run.font.name = 'Arial'
    run.bold = bold
    if color:
        run.font.color.rgb = RGBColor(*color)
    if align:
        p.alignment = align
    p.paragraph_format.space_after = Pt(space_after)
    return p


def section_heading(doc, number, title):
    """Add a numbered section heading."""
    p = doc.add_paragraph()
    run = p.add_run(f"{number}. {title}")
    run.font.size = Pt(13)
    run.font.name = 'Arial'
    run.bold = True
    run.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)
    p.paragraph_format.space_before = Pt(18)
    p.paragraph_format.space_after = Pt(8)
    return p


def bullet_point(doc, text, indent=0.5):
    """Add a bullet point."""
    p = doc.add_paragraph(style='List Bullet')
    p.text = text
    for run in p.runs:
        run.font.size = Pt(11)
        run.font.name = 'Arial'
    return p


def add_table(doc, headers, rows):
    """Add a formatted table."""
    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = 'Table Grid'

    # Header row
    for i, h in enumerate(headers):
        cell = table.rows[0].cells[i]
        cell.text = h
        for p in cell.paragraphs:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for run in p.runs:
                run.bold = True
                run.font.size = Pt(10)
                run.font.name = 'Arial'
        set_cell_shading(cell, 'D6E4F0')

    # Data rows
    for ri, row in enumerate(rows):
        for ci, val in enumerate(row):
            cell = table.rows[ri + 1].cells[ci]
            cell.text = str(val)
            for p in cell.paragraphs:
                for run in p.runs:
                    run.font.size = Pt(10)
                    run.font.name = 'Arial'

    return table


# --- Float Storage SOP ---

def generate_float_storage_sop(project):
    """Generate Float Storage SOP .docx for a project."""
    doc = Document()
    pid = project.get("id", "PXXX")
    name = project.get("name", "Project")
    full_name = project.get("full_name", name)
    capacity = project.get("capacity_mw", "XX")
    epc = project.get("epc", "EPC Contractor")
    site = project.get("site_location") or project.get("reservoir_name") or "Site Location"
    client = project.get("client_name") or epc
    doc_no = f"FSR-{pid}-SOP-SM-01_R0"

    project_title = f"{capacity} MW FLOATING SOLAR PV PROJECT\n{full_name}"

    add_header_footer(doc, project_title, doc_no)

    # --- Cover Page ---
    doc.add_paragraph()
    doc.add_paragraph()
    styled_para(doc, project_title, size=16, bold=True, color=(0x1F, 0x4E, 0x79), align=WD_ALIGN_PARAGRAPH.CENTER, space_after=12)
    styled_para(doc, "SOP for Float Storage", size=14, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=8)
    styled_para(doc, doc_no, size=12, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=24)

    # Revision table
    add_table(doc,
        ["Rev", "Date", "Written By", "Checked By", "Approved By", "Description"],
        [["R0", "—", "—", "—", "—", "Issued for Information"]]
    )

    doc.add_paragraph()
    doc.add_paragraph()
    styled_para(doc, "SOP FOR FLOAT STORAGE", size=18, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)

    doc.add_page_break()

    # --- Contents ---
    styled_para(doc, "Contents", size=14, bold=True, color=(0x1F, 0x4E, 0x79))
    for i, title in enumerate([
        "INTRODUCTION", "SCOPE", "STORAGE LOCATION",
        "STORAGE METHODOLOGY", "STORAGE PROCEDURE", "STORAGE AREA SAFETY"
    ], 1):
        styled_para(doc, f"{i}. {title}", size=11)

    doc.add_page_break()

    # --- Section 1: Introduction (PROJECT-SPECIFIC) ---
    section_heading(doc, 1, "INTRODUCTION")

    styled_para(doc, (
        f"This Standard Operating Procedure (SOP) outlines the methodology and procedure "
        f"for storage of HDPE floaters at the project site for the {full_name} project."
    ))

    styled_para(doc, "Project Particulars", size=11, bold=True, space_after=4)
    add_table(doc,
        ["Particular", "Description"],
        [
            ["Project Name", full_name],
            ["Plant AC Capacity", f"{capacity} MW"],
            ["Project ID", pid],
            ["EPC Contractor", epc],
            ["Site Location", site],
            ["Client / Owner", client],
            ["Design Life", "25 years"],
            ["Floatex Scope", "Float system design, supply & supervision"],
        ]
    )

    styled_para(doc, (
        f"\nThrough the contractual process, the development of the {capacity} MW floating solar "
        f"PV project has been awarded to {epc}. {epc} has subcontracted the floating system "
        f"scope of work to Floatex Solar Pvt. Ltd."
    ), space_after=12)

    # --- Section 2: Scope (STANDARD) ---
    section_heading(doc, 2, "SCOPE")
    styled_para(doc, (
        "This document briefs the methodology and the procedure of storage of floaters "
        "being used for the project, as mentioned in Section 1."
    ))

    # --- Section 3: Storage Location (STANDARD) ---
    section_heading(doc, 3, "STORAGE LOCATION")
    styled_para(doc, (
        "Approximate 1 hectare area is required for storage of 40 MWp of floaters for the project. "
        "It is recommended to develop at least 50 MWp of storage area at two different locations "
        "near to the launch pad."
    ))
    styled_para(doc, (
        f"Development of storage area and location is in the scope of {epc}."
    ))
    styled_para(doc, (
        "The storage area layout should accommodate the following zones:"
    ))
    bullet_point(doc, "Float unloading zone (vehicle access)")
    bullet_point(doc, "Stacking zone (organized by float type)")
    bullet_point(doc, "Buffer zone (minimum 2m between stacks and boundary)")
    bullet_point(doc, "Access pathways for material handling equipment")

    # --- Section 4: Storage Methodology (STANDARD) ---
    section_heading(doc, 4, "STORAGE METHODOLOGY")
    styled_para(doc, (
        "The following methodology is adopted while storing the floats in the designated area:"
    ))
    bullet_point(doc, "The designated area must be cleaned and levelled prior to float storage. "
                      "The ground surface should be free from any sharp object, stone etc. which can damage the float.")
    bullet_point(doc, "Floats must be placed in stacked manner")
    bullet_point(doc, "Four numbers of floats must be strapped together to make one stack")
    bullet_point(doc, "Four such stacks will be kept on top of each other")
    bullet_point(doc, "The height of each stack should ensure that the floats in stacks are stable")
    bullet_point(doc, "Floats of the same type must be stored together — do not mix Panel Floats, "
                      "Aisle Floats, and Side Floats in the same stack")

    # --- Section 5: Storage Procedure (STANDARD) ---
    section_heading(doc, 5, "STORAGE PROCEDURE")
    styled_para(doc, (
        "The following procedure must be ensured for proper handling and storage "
        "of the floaters in the storage area:"
    ))
    bullet_point(doc, "Proper care should be given while unloading the floats from the vehicle "
                      "to ensure no damage occurs")
    bullet_point(doc, "Floats must NOT be thrown from the vehicle")
    bullet_point(doc, "Floats must NOT be dragged to the final stack location — instead should be "
                      "lifted and placed on the stack")
    bullet_point(doc, "Inspect each float visually during unloading for cracks, deformation, or damage")
    bullet_point(doc, "Damaged floats must be separated and reported immediately")
    bullet_point(doc, "Maintain a count register for each delivery — verify quantity against challan")

    # --- Section 6: Storage Area Safety (STANDARD) ---
    section_heading(doc, 6, "STORAGE AREA SAFETY")
    styled_para(doc, (
        "The following measures must be ensured for the safety of the storage area:"
    ))
    bullet_point(doc, "The designated area must be fenced from all sides except the main entry")
    bullet_point(doc, "Security must be deployed for the protection of the floaters")
    bullet_point(doc, "No smoking or open flames within 50m of the storage area (HDPE is flammable)")
    bullet_point(doc, "Fire extinguishers must be placed at entry and exit points")
    bullet_point(doc, "Adequate lighting for night-time security")
    bullet_point(doc, "Drainage must be ensured — waterlogging can displace stacks")

    # --- Footer ---
    doc.add_paragraph()
    styled_para(doc, "— End of Document —", size=10, align=WD_ALIGN_PARAGRAPH.CENTER, color=(0x99, 0x99, 0x99))
    styled_para(doc, f"FLOATEX DOC. NO.: {doc_no}", size=9, align=WD_ALIGN_PARAGRAPH.CENTER, color=(0x99, 0x99, 0x99))

    # Save to bytes
    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    return buffer, doc_no


# --- Template registry ---

TEMPLATES = {
    "float-storage-sop": {
        "title": "Float Storage SOP",
        "generator": generate_float_storage_sop,
        "filename_pattern": "FSR-{pid}-SOP-SM-01_R0.docx",
    },
    # More templates will be added here
}
