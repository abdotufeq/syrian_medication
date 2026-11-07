"""Manufacturer site health checking and reporting."""

from __future__ import annotations

import logging

import requests
from bs4 import BeautifulSoup

from .constants import HEALTH_CHECK_TIMEOUT
from .http_client import create_session


logger = logging.getLogger(__name__)


def check_site_health(site: dict) -> dict:
    """Check if a manufacturer site is accessible and has product data.
    
    Returns a dict with status information about the site.
    Tries SSL bypass if initial connection fails with SSL error.
    """
    result = {
        "name": site["english_name"],
        "website": site.get("website", ""),
        "status": "unknown",
        "message": "",
        "product_count": 0,
    }
    
    website = site.get("website")
    if not website:
        result["status"] = "no_website"
        result["message"] = "No website URL configured"
        return result
    
    # Check main website - try with SSL first, then without
    ssl_error = None
    for verify_ssl in [True, False]:
        try:
            session = create_session()
            response = session.get(website, timeout=HEALTH_CHECK_TIMEOUT, verify=verify_ssl)
            
            if response.status_code == 403:
                result["status"] = "forbidden"
                result["message"] = "403 Forbidden - site may block scrapers"
                return result
            elif response.status_code == 500:
                result["status"] = "error"
                result["message"] = "500 Internal Server Error - site is down"
                return result
            elif response.status_code >= 400:
                result["status"] = "error"
                result["message"] = f"HTTP {response.status_code}"
                return result
            
            if not verify_ssl:
                result["message"] = "SSL cert issue (works with verify=False)"
            
            result["status"] = "accessible"
            
            # Check product catalog if configured
            seeds = site.get("seeds", [])
            if seeds:
                soup = BeautifulSoup(response.text, "html.parser")
                total_links = len(soup.find_all("a", href=True))
                if result["message"]:
                    result["message"] += f", {total_links} links on homepage"
                else:
                    result["message"] = f"OK - {total_links} links on homepage"
                
                # Try to count product links from seed pages
                for seed_url in seeds[:1]:  # Check first seed only
                    try:
                        seed_response = session.get(seed_url, timeout=HEALTH_CHECK_TIMEOUT, verify=verify_ssl)
                        seed_soup = BeautifulSoup(seed_response.text, "html.parser")
                        product_links = [
                            a for a in seed_soup.find_all("a", href=True)
                            if any(kw in a.get("href", "").lower() for kw in ["product", "medicament", "drug"])
                        ]
                        result["product_count"] = len(product_links)
                        result["message"] += f", ~{len(product_links)} product links on catalog"
                    except requests.RequestException as e:
                        result["message"] += f" (seed page error: {type(e).__name__})"
            break  # Success, exit the verify_ssl loop
            
        except requests.exceptions.SSLError as e:
            ssl_error = e
            if verify_ssl:
                continue  # Try without SSL verification
            else:
                result["status"] = "ssl_error"
                result["message"] = f"SSL error even with verify=False"
                break
        except requests.exceptions.ConnectionError:
            result["status"] = "connection_error"
            result["message"] = "Cannot connect to site"
            return result
        except requests.exceptions.Timeout:
            result["status"] = "timeout"
            result["message"] = "Site timed out"
            return result
        except requests.RequestException as e:
            result["status"] = "error"
            result["message"] = f"Request error: {type(e).__name__}"
            return result
    
    return result


def check_all_sites(sites: list[dict] | None = None) -> list[dict]:
    """Check health of all manufacturer sites and print a summary report."""
    if sites is None:
        sites = SITES
    
    logger.info("="*60)
    logger.info("Checking %d manufacturer sites...", len(sites))
    logger.info("="*60)
    
    results = []
    for site in sites:
        result = check_site_health(site)
        results.append(result)
        
        # Print status with icon
        icon = {
            "accessible": "✓",
            "no_website": "○",
            "forbidden": "✗",
            "connection_error": "✗",
            "timeout": "✗",
            "error": "✗",
            "ssl_error": "⚠",
        }.get(result["status"], "?")
        
        logger.info("  %s %-45s %-15s %s", icon, result["name"], result["status"], result["message"])
    
    # Summary
    accessible = sum(1 for r in results if r["status"] == "accessible")
    no_website = sum(1 for r in results if r["status"] == "no_website")
    errors = sum(1 for r in results if r["status"] not in ("accessible", "no_website"))
    
    logger.info("="*60)
    logger.info("Summary: %d accessible, %d no website, %d errors (out of %d total)", 
                accessible, no_website, errors, len(results))
    logger.info("="*60)
    
    return results