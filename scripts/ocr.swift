import Foundation
import PDFKit
import Vision
import AppKit
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let pdf = PDFDocument(url: url), pdf.pageCount <= 3 else { exit(2) }
for index in 0..<pdf.pageCount {
    guard let page = pdf.page(at: index) else { continue }
    let bounds = page.bounds(for: .mediaBox)
    let scale = min(2.5, 1800 / max(bounds.width, bounds.height))
    let width = max(1, Int(bounds.width * scale)), height = max(1, Int(bounds.height * scale))
    guard let context = CGContext(data:nil,width:width,height:height,bitsPerComponent:8,bytesPerRow:width*4,space:CGColorSpaceCreateDeviceRGB(),bitmapInfo:CGImageAlphaInfo.premultipliedLast.rawValue) else { exit(3) }
    context.setFillColor(CGColor(gray:1,alpha:1));context.fill(CGRect(x:0,y:0,width:width,height:height))
    context.scaleBy(x:scale,y:scale);context.translateBy(x:-bounds.minX,y:-bounds.minY);page.draw(with:.mediaBox,to:context)
    guard let image = context.makeImage() else { exit(4) }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate;request.recognitionLanguages = ["en-US"]
    do {
        try VNImageRequestHandler(cgImage:image,options:[:]).perform([request])
        for result in request.results ?? [] { if let text=result.topCandidates(1).first?.string { print(text) } }
    } catch { exit(5) }
}
