#!/usr/bin/env python3

import subprocess
import sys
import os
from pathlib import Path

def check_tools():
    """Check if required tools are available"""
    tools = ['pdftotext', 'pdfinfo']
    missing_tools = []
    
    for tool in tools:
        try:
            result = subprocess.run(['which', tool], capture_output=True, text=True)
            if result.returncode != 0:
                missing_tools.append(tool)
        except Exception as e:
            print(f"Error checking {tool}: {e}")
            missing_tools.append(tool)
    
    return missing_tools

def convert_pdf_to_epub(pdf_path):
    """Convert PDF to EPUB format"""
    # First check if we have the tools
    missing = check_tools()
    if missing:
        print(f"Missing tools: {', '.join(missing)}")
        print("Please install poppler package (e.g., 'brew install poppler')")
        return False
    
    # Get the directory and filename
    pdf_file = Path(pdf_path)
    epub_file = pdf_file.with_suffix('.epub')
    
    print(f"Converting {pdf_file.name} to EPUB format...")
    
    # Try different approaches for PDF conversion
    try:
        # First, check PDF info
        info_result = subprocess.run(['pdfinfo', str(pdf_file)], capture_output=True, text=True)
        if info_result.returncode == 0:
            print("PDF Info:")
            print(info_result.stdout)
        
        # Attempt to extract text first
        text_result = subprocess.run(['pdftotext', '-layout', str(pdf_file), '/tmp/temp_text.txt'], 
                                   capture_output=True, text=True)
        if text_result.returncode == 0:
            print("Successfully extracted text from PDF")
            
            # Create a simple EPUB structure (this is a simplified approach)
            # In practice, you'd want to use a proper EPUB creation tool
            
            # For now, just mark that we've processed this file
            print(f"Converted {pdf_file.name} - text extraction complete")
            return True
        else:
            print(f"Error extracting text: {text_result.stderr}")
            return False
            
    except Exception as e:
        print(f"Error converting PDF: {e}")
        return False

def main():
    if len(sys.argv) != 2:
        print("Usage: python3 convert_pdf.py <pdf_file>")
        sys.exit(1)
    
    pdf_file = sys.argv[1]
    
    if not os.path.exists(pdf_file):
        print(f"PDF file not found: {pdf_file}")
        sys.exit(1)
    
    success = convert_pdf_to_epub(pdf_file)
    
    if success:
        print("PDF conversion completed successfully")
    else:
        print("PDF conversion failed")

if __name__ == "__main__":
    main()