#!/usr/bin/env python3

import sys
import re
import socket
import smtplib
import dns.resolver
import concurrent.futures
from typing import List, Tuple, Dict
import time
import threading
from collections import defaultdict

# Thread-safe cache for DNS results
_dns_cache = {}
_cache_lock = threading.Lock()

def is_valid_domain_syntax(domain: str) -> bool:
    """Check if domain has valid syntax"""
    pattern = r'^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$'
    return re.match(pattern, domain.strip()) is not None and len(domain) <= 253

# def get_dns_records_cached(domain: str, record_type: str) -> List:
#     """Get DNS records with caching to avoid duplicate lookups"""
#     cache_key = f"{domain}:{record_type}"
    
#     with _cache_lock:
#         if cache_key in _dns_cache:
#             return _dns_cache[cache_key]
    
#     try:
#         if record_type == 'MX':
#             records = dns.resolver.resolve(domain, 'MX')
#             result = [(int(mx.preference), str(mx.exchange).rstrip('.')) for mx in records]
#         elif record_type == 'A':
#             records = dns.resolver.resolve(domain, 'A')
#             result = [str(record) for record in records]
#         else:
#             result = []
#     except Exception:
#         result = []
    
#     with _cache_lock:
#         _dns_cache[cache_key] = result
    
#     return result

def get_dns_records_cached(domain: str, record_type: str) -> List:
    """Get DNS records with multiple DNS server fallback"""
    cache_key = f"{domain}:{record_type}"
    
    with _cache_lock:
        if cache_key in _dns_cache:
            return _dns_cache[cache_key]
    
    # Try multiple DNS servers in order
    dns_configs = [
        None,  # System default DNS
        ['8.8.8.8', '8.8.4.4'],  # Google DNS
        ['1.1.1.1', '1.0.0.1'],  # Cloudflare DNS
        ['208.67.222.222', '208.67.220.220'],  # OpenDNS
        ['9.9.9.9', '149.112.112.112']  # Quad9 DNS
    ]
    
    result = []
    last_error = None
    
    for i, dns_config in enumerate(dns_configs):
        try:
            resolver = dns.resolver.Resolver()
            
            if dns_config:
                resolver.nameservers = dns_config
                print(f"  Trying DNS server: {dns_config[0]} for {domain}")
            else:
                print(f"  Trying system DNS for {domain}")
            
            # Set reasonable timeouts
            resolver.timeout = 3
            resolver.lifetime = 10
            
            if record_type == 'MX':
                records = resolver.resolve(domain, 'MX')
                result = [(int(mx.preference), str(mx.exchange).rstrip('.')) for mx in records]
            elif record_type == 'A':
                records = resolver.resolve(domain, 'A')
                result = [str(record) for record in records]
            else:
                result = []
            
            if result:  # If we got results, break out of the loop
                print(f"  ✅ Success with {dns_config[0] if dns_config else 'system DNS'}")
                break
                
        except Exception as e:
            last_error = str(e)
            print(f"  ❌ Failed with {dns_config[0] if dns_config else 'system DNS'}: {e}")
            continue  # Try next DNS server
    
    if not result and last_error:
        print(f"  ⚠️  All DNS servers failed for {domain}. Last error: {last_error}")
    
    with _cache_lock:
        _dns_cache[cache_key] = result
    
    return result


def get_mx_records(domain: str) -> List[Tuple[int, str]]:
    """Get all MX records for domain with their priorities"""
    return get_dns_records_cached(domain, 'MX')

def get_a_record(domain: str) -> bool:
    """Check if domain has A record (IP address)"""
    return bool(get_dns_records_cached(domain, 'A'))

def test_smtp_connection_fast(mx_server: str, timeout: int = 5) -> Tuple[bool, str]:
    """Fast SMTP connection test with reduced timeout"""
    try:
        # Use socket for faster connection test
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((mx_server, 25))
        sock.close()
        
        if result == 0:
            return True, "SMTP port accessible"
        else:
            return False, "SMTP port not accessible"
            
    except socket.timeout:
        return False, "Connection timeout"
    except socket.gaierror:
        return False, "Cannot resolve server"
    except Exception as e:
        return False, f"Connection error: {str(e)[:30]}"

def validate_domain_fast(domain: str) -> Tuple[str, bool, str, Dict]:
    """Fast domain email capability validation"""
    domain = domain.strip().lower()
    results = {
        'syntax': False,
        'a_record': False,
        'mx_records': [],
        'smtp_connection': False,
        'details': {}
    }
    
    if not domain:
        return domain, False, "Empty domain", results
    
    # Clean domain
    domain = domain.replace('http://', '').replace('https://', '').replace('www.', '')
    domain = domain.split('/')[0]  # Remove any path
    
    # 1. Syntax check
    if not is_valid_domain_syntax(domain):
        return domain, False, "Invalid domain syntax", results
    results['syntax'] = True
    
    # 2. Get DNS records in parallel
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            mx_future = executor.submit(get_mx_records, domain)
            a_future = executor.submit(get_a_record, domain)
            
            # Wait for both with timeout
            mx_records = mx_future.result(timeout=10)
            has_a_record = a_future.result(timeout=10)
    except concurrent.futures.TimeoutError:
        return domain, False, "DNS lookup timeout", results
    except Exception as e:
        return domain, False, f"DNS lookup failed: {str(e)[:30]}", results
    
    results['a_record'] = has_a_record
    results['mx_records'] = mx_records
    
    if not has_a_record:
        return domain, False, "Domain does not exist", results
    
    if not mx_records:
        return domain, False, "No mail servers configured", results
    
    results['details']['mx_servers'] = [f"{priority}: {server}" for priority, server in mx_records]
    
    # 3. Quick SMTP test on primary MX
    primary_mx = min(mx_records, key=lambda x: x[0])[1]
    smtp_works, smtp_reason = test_smtp_connection_fast(primary_mx, timeout=3)
    results['smtp_connection'] = smtp_works
    results['details']['smtp_test'] = smtp_reason
    
    if smtp_works:
        return domain, True, f"✅ Can receive emails - {smtp_reason}", results
    else:
        return domain, False, f"❌ Mail server not accessible - {smtp_reason}", results

def validate_domains_batch(domains_batch: List[str]) -> List[Tuple]:
    """Validate a batch of domains"""
    results = []
    for domain in domains_batch:
        result = validate_domain_fast(domain)
        results.append(result)
    return results

def validate_domains_from_file_optimized(filename: str, max_workers: int = 50) -> None:
    """Optimized validation with concurrent processing"""
    try:
        with open(filename, 'r', encoding='utf-8') as file:
            domains = [line.strip() for line in file if line.strip()]
    except FileNotFoundError:
        print(f"Error: File '{filename}' not found")
        sys.exit(1)
    except Exception as e:
        print(f"Error reading file: {e}")
        sys.exit(1)
    
    if not domains:
        print("No domains found in file")
        return
    
    # Remove duplicates while preserving order
    seen = set()
    unique_domains = []
    for domain in domains:
        domain_clean = domain.lower().strip()
        if domain_clean not in seen:
            seen.add(domain_clean)
            unique_domains.append(domain)
    
    print(f"Validating email capabilities for {len(unique_domains)} unique domains...")
    print(f"Using {max_workers} concurrent workers for faster processing...")
    print("⚠️  This checks if domains can receive emails, not specific mailboxes.\n")
    
    start_time = time.time()
    valid_count = 0
    invalid_count = 0
    results = []
    
    # Process domains in batches with progress tracking
    batch_size = max(1, len(unique_domains) // 20)  # 20 progress updates
    completed = 0
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all domains for processing
        future_to_domain = {
            executor.submit(validate_domain_fast, domain): domain 
            for domain in unique_domains
        }
        
        # Process results as they complete
        for future in concurrent.futures.as_completed(future_to_domain):
            domain = future_to_domain[future]
            try:
                result = future.result(timeout=30)  # 30 second timeout per domain
                results.append(result)
                
                if result[1]:  # is_valid
                    valid_count += 1
                else:
                    invalid_count += 1
                
                completed += 1
                
                # Show progress every batch_size completions
                if completed % batch_size == 0 or completed == len(unique_domains):
                    elapsed = time.time() - start_time
                    rate = completed / elapsed if elapsed > 0 else 0
                    eta = (len(unique_domains) - completed) / rate if rate > 0 else 0
                    print(f"Progress: {completed}/{len(unique_domains)} ({completed/len(unique_domains)*100:.1f}%) "
                          f"- Rate: {rate:.1f}/sec - ETA: {eta:.0f}s")
                
            except concurrent.futures.TimeoutError:
                results.append((domain, False, "Processing timeout", {}))
                invalid_count += 1
                completed += 1
            except Exception as e:
                results.append((domain, False, f"Processing error: {str(e)[:30]}", {}))
                invalid_count += 1
                completed += 1
    
    # Sort results to match original order
    domain_to_result = {result[0]: result for result in results}
    sorted_results = [domain_to_result.get(domain.lower().strip(), (domain, False, "Not processed", {})) 
                     for domain in unique_domains]
    
    total_time = time.time() - start_time
    
    # Display results summary first
    print("\n" + "=" * 100)
    print("SUMMARY:")
    print("=" * 100)
    print(f"Total domains processed: {len(unique_domains)}")
    print(f"✅ Can receive emails: {valid_count}")
    print(f"❌ Cannot receive emails: {invalid_count}")
    print(f"Email-capable rate: {(valid_count/len(unique_domains)*100):.1f}%")
    print(f"Processing time: {total_time:.1f} seconds")
    print(f"Average rate: {len(unique_domains)/total_time:.1f} domains/second")
    
    # Categorize results
    print(f"\nCATEGORIES:")
    categories = defaultdict(int)
    for domain, is_valid, reason, details in sorted_results:
        if "no mail servers" in reason.lower() or "no mx" in reason.lower():
            category = "No mail servers configured"
        elif "does not exist" in reason.lower():
            category = "Domain doesn't exist"
        elif "not accessible" in reason.lower() or "not responding" in reason.lower():
            category = "Mail server offline/blocked"
        elif "invalid domain syntax" in reason.lower():
            category = "Invalid domain format"
        elif "timeout" in reason.lower():
            category = "Timeout errors"
        elif is_valid:
            category = "Can receive emails"
        else:
            category = "Other issues"
        
        categories[category] += 1
    
    for category, count in sorted(categories.items()):
        percentage = (count / len(unique_domains)) * 100
        print(f"  {category}: {count} ({percentage:.1f}%)")
    
    # Save detailed results
    output_filename = filename.replace('.txt', '_domain_email_results.txt')
    with open(output_filename, 'w', encoding='utf-8') as f:
        f.write("Domain Email Capability Validation Results\n")
        f.write("=" * 100 + "\n")
        f.write(f"Processed: {len(unique_domains)} domains in {total_time:.1f} seconds\n")
        f.write(f"Rate: {len(unique_domains)/total_time:.1f} domains/second\n\n")
        
        for domain, is_valid, reason, details in sorted_results:
            status = "CAN_RECEIVE_EMAILS" if is_valid else "CANNOT_RECEIVE_EMAILS"
            f.write(f"{status:<20} | {domain:<35} | {reason}\n")
            
            if details.get('mx_servers'):
                f.write(f"                     └─ Mail Servers: {', '.join(details['mx_servers'])}\n")
            if details.get('smtp_test'):
                f.write(f"                     └─ SMTP Test: {details['smtp_test']}\n")
        
        f.write(f"\nSummary:\n")
        f.write(f"Total: {len(unique_domains)}, Can receive emails: {valid_count}, Cannot: {invalid_count}\n")
        f.write(f"Success rate: {(valid_count/len(unique_domains)*100):.1f}%\n\n")
        
        f.write("Categories:\n")
        for category, count in sorted(categories.items()):
            percentage = (count / len(unique_domains)) * 100
            f.write(f"  {category}: {count} ({percentage:.1f}%)\n")
    
    print(f"\nDetailed results saved to: {output_filename}")
    
    # Show top failed domains for debugging
    failed_domains = [(domain, reason) for domain, is_valid, reason, details in sorted_results if not is_valid]
    if failed_domains:
        print(f"\nSample of failed domains (showing first 10):")
        for i, (domain, reason) in enumerate(failed_domains[:10]):
            print(f"  {i+1}. {domain}: {reason}")

def main():
    if len(sys.argv) not in [2, 3]:
        print("Usage: python3 main.py <domains.txt> [max_workers]")
        print("Example: python3 main.py domains.txt 50")
        print("\nDomains file should contain one domain per line:")
        print("gmail.com")
        print("yahoo.com")
        print("example.com")
        print("\nOptional max_workers (default: 50): Number of concurrent threads")
        print("For 900 domains, try values between 30-100 depending on your connection")
        sys.exit(1)
    
    filename = sys.argv[1]
    max_workers = int(sys.argv[2]) if len(sys.argv) == 3 else 50
    
    # Validate max_workers
    if max_workers < 1 or max_workers > 200:
        print("Warning: max_workers should be between 1-200. Using 50.")
        max_workers = 50
    
    # Check if dnspython is installed
    try:
        import dns.resolver
    except ImportError:
        print("Error: dnspython is required for this script")
        print("Install it with: pip3 install dnspython")
        sys.exit(1)
    
    print(f"Starting optimized domain validation with {max_workers} workers...")
    validate_domains_from_file_optimized(filename, max_workers)

if __name__ == "__main__":
    main()
