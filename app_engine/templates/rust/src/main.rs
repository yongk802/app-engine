use std::env;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};

fn handle(mut stream: TcpStream) -> std::io::Result<()> {
    let mut request = [0_u8; 1024];
    let size = stream.read(&mut request)?;
    let request = String::from_utf8_lossy(&request[..size]);
    let ready = request.starts_with("GET / ") || request.starts_with("GET /health ");
    let status = if ready { "200 OK" } else { "404 Not Found" };
    let body = format!(r#"{{"app":"{{APP_ID}}","ready":{ready}}}"#);
    write!(
        stream,
        "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
}

fn main() -> std::io::Result<()> {
    let port = env::var("PORT").unwrap_or_else(|_| "8000".into());
    let listener = TcpListener::bind(format!("127.0.0.1:{port}"))?;
    println!("{{APP_LABEL}} listening on {port}");
    for stream in listener.incoming().flatten() {
        let _ = handle(stream);
    }
    Ok(())
}
