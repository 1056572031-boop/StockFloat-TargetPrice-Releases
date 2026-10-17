// Tool-only ScreenCaptureKit recorder. No app bundle or product behavior changes.
import Foundation
import ScreenCaptureKit
import AVFoundation
import CoreMedia
import CoreVideo
import CoreGraphics
import ImageIO
import CoreImage

func hostSeconds() -> Double {
    var info = mach_timebase_info_data_t()
    mach_timebase_info(&info)
    return Double(mach_absolute_time()) * Double(info.numer) / Double(info.denom) / 1e9
}

func writeJSON(_ value: Any, _ path: URL) throws {
    let data = try JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys])
    try data.write(to: path, options: .atomic)
}

enum ProbeError: Error { case invalid(String) }

struct PixelPoint {
    let x: Int
    let y: Int
    let r: Int
    let g: Int
    let b: Int
}

struct PriceMask {
    var positive: [PixelPoint] = []
    var negative: [(Int, Int)] = []
    let width = 96
    let height = 33

    init(bytes: [UInt8], width: Int, height: Int) throws {
        guard width == 184 && height >= 105 else { throw ProbeError.invalid("template must be a 184pt native window at 1x") }
        for y in 72..<105 {
            for x in 12..<108 {
                let i = (y * width + x) * 4
                let r = Int(bytes[i]), g = Int(bytes[i + 1]), b = Int(bytes[i + 2])
                if bytes[i + 3] >= 245 && max(r, g, b) > 170 {
                    positive.append(PixelPoint(x: x - 12, y: y - 72, r: r, g: g, b: b))
                } else if max(r, g, b) < 120 {
                    negative.append((x - 12, y - 72))
                }
            }
        }
        guard positive.count >= 150 && negative.count >= 150 else {
            throw ProbeError.invalid("template has insufficient price foreground or background")
        }
        negative = stride(from: 0, to: negative.count, by: max(1, negative.count / 48)).map { negative[$0] }
    }

    func locate(_ bytes: UnsafePointer<UInt8>, _ w: Int, _ h: Int, _ strideBytes: Int,
                near: (Int, Int)?) -> [String: Any] {
        guard w >= width && h >= height else { return ["visible": false] }
        let anchors = stride(from: 0, to: positive.count, by: max(1, positive.count / 32)).map { positive[$0] }
        func hit(_ point: PixelPoint, _ x: Int, _ y: Int) -> Bool {
            let i = (y + point.y) * strideBytes + (x + point.x) * 4
            let b = Int(bytes[i]), g = Int(bytes[i + 1]), r = Int(bytes[i + 2])
            return max(r, g, b) > 150 && max(abs(r - point.r), abs(g - point.g), abs(b - point.b)) <= 75
        }
        func scan(_ x0: Int, _ x1: Int, _ y0: Int, _ y1: Int) -> (Int, Int, Double)? {
            guard x0 <= x1 && y0 <= y1 else { return nil }
            var best: (Int, Int, Double)?
            for y in y0...y1 {
                for x in x0...x1 {
                    var count = 0
                    for point in anchors { if hit(point, x, y) { count += 1 } }
                    if Double(count) / Double(anchors.count) < 0.75 { continue }
                    let matches = positive.reduce(0) { $0 + (hit($1, x, y) ? 1 : 0) }
                    let fraction = Double(matches) / Double(positive.count)
                    if fraction < 0.65 { continue }
                    let dark = negative.reduce(0) { n, point in
                        let i = (y + point.1) * strideBytes + (x + point.0) * 4
                        return n + (max(bytes[i], bytes[i + 1], bytes[i + 2]) < 140 ? 1 : 0)
                    }
                    if Double(dark) / Double(negative.count) >= 0.70 {
                        if best == nil || fraction > best!.2 { best = (x, y, fraction) }
                        if fraction >= 0.98 { return best }
                    }
                }
            }
            return best
        }
        var found: (Int, Int, Double)?
        if let (x, y) = near {
            found = scan(max(0, x - 12), min(w - width, x + 12),
                         max(0, y - 12), min(h - height, y + 12))
        }
        if found == nil { found = scan(0, w - width, 0, h - height) }
        if let (x, y, fraction) = found {
            return ["visible": true, "window_x": x - 12, "window_y": y - 72,
                    "matched_fraction": fraction]
        }
        return ["visible": false]
    }
}

func rgbaImage(_ url: URL) throws -> (Int, Int, [UInt8]) {
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw ProbeError.invalid("could not read image template")
    }
    var bytes = [UInt8](repeating: 0, count: image.width * image.height * 4)
    let okay = bytes.withUnsafeMutableBytes { data -> Bool in
        guard let context = CGContext(data: data.baseAddress, width: image.width, height: image.height,
            bitsPerComponent: 8, bytesPerRow: image.width * 4, space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGBitmapInfo.byteOrder32Big.rawValue | CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
        context.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
        return true
    }
    guard okay else { throw ProbeError.invalid("template pixel conversion failed") }
    return (image.width, image.height, bytes)
}

func decode(_ video: URL, _ template: URL, _ output: URL) async throws {
    let (width, height, rgba) = try rgbaImage(template)
    let mask = try PriceMask(bytes: rgba, width: width, height: height)
    let asset = AVURLAsset(url: video)
    guard let track = try await asset.loadTracks(withMediaType: .video).first else { throw ProbeError.invalid("video track missing") }
    let reader = try AVAssetReader(asset: asset)
    let outputTrack = AVAssetReaderTrackOutput(track: track,
        outputSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
    outputTrack.alwaysCopiesSampleData = false
    guard reader.canAdd(outputTrack) else { throw ProbeError.invalid("video decoder rejected track") }
    reader.add(outputTrack)
    guard reader.startReading() else { throw ProbeError.invalid("video decoder did not start") }
    var rows: [[String: Any]] = []
    var near: (Int, Int)?
    var previousVisibility: Bool?
    var imagesWritten = 0
    let imageContext = CIContext()
    let imageDirectory = output.deletingLastPathComponent().appendingPathComponent("frames")
    try FileManager.default.createDirectory(at: imageDirectory, withIntermediateDirectories: true)
    while let sample = outputTrack.copyNextSampleBuffer() {
        try autoreleasepool {
            guard let buffer = CMSampleBufferGetImageBuffer(sample) else { throw ProbeError.invalid("decoded frame missing pixels") }
            CVPixelBufferLockBaseAddress(buffer, .readOnly)
            defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
            guard let base = CVPixelBufferGetBaseAddress(buffer) else { throw ProbeError.invalid("decoded pixels inaccessible") }
            var row = mask.locate(base.assumingMemoryBound(to: UInt8.self), CVPixelBufferGetWidth(buffer),
                                  CVPixelBufferGetHeight(buffer), CVPixelBufferGetBytesPerRow(buffer), near: near)
            row["movie_pts"] = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sample))
            if let x = row["window_x"] as? Int, let y = row["window_y"] as? Int { near = (x + 12, y + 72) }
            let visible = row["visible"] as? Bool == true
            if previousVisibility != visible && imagesWritten < 24 {
                let image = CIImage(cvPixelBuffer: buffer)
                let name = String(format: "frame-%05d.png", rows.count)
                let url = imageDirectory.appendingPathComponent(name)
                guard let cg = imageContext.createCGImage(image, from: image.extent),
                      let sink = CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil) else {
                    throw ProbeError.invalid("could not export visibility transition frame")
                }
                CGImageDestinationAddImage(sink, cg, nil)
                guard CGImageDestinationFinalize(sink) else { throw ProbeError.invalid("transition frame export incomplete") }
                row["image"] = "frames/" + name
                imagesWritten += 1
            }
            previousVisibility = visible
            rows.append(row)
        }
    }
    guard reader.status == .completed else { throw ProbeError.invalid("video decode incomplete: \(String(describing: reader.error))") }
    try writeJSON(["frames": rows, "decoder_completed": true, "template_pixels": mask.positive.count], output)
}

func selfTest() throws {
    let w = 184, h = 224
    var image = [UInt8](repeating: 0, count: w * h * 4)
    for i in stride(from: 3, to: image.count, by: 4) { image[i] = 255 }
    for x0 in [20, 35, 50, 65] {
        for y in 80..<96 { for x in x0..<(x0 + 7) {
            let i = (y * w + x) * 4
            image[i] = 235; image[i + 1] = 235; image[i + 2] = 235
        } }
    }
    let mask = try PriceMask(bytes: image, width: w, height: h)
    // Synthetic RGBA is also BGRA here because every test color is neutral grey.
    let positive = image.withUnsafeBufferPointer { mask.locate($0.baseAddress!, w, h, w * 4, near: nil) }
    guard positive["visible"] as? Bool == true else { throw ProbeError.invalid("clean text failed") }
    for fill in [UInt8(0), UInt8(110), UInt8(235)] {
        let blank = [UInt8](repeating: fill, count: image.count)
        let result = blank.withUnsafeBufferPointer { mask.locate($0.baseAddress!, w, h, w * 4, near: nil) }
        guard result["visible"] as? Bool == false else { throw ProbeError.invalid("blank false positive") }
    }
    var colored = image
    for i in stride(from: 0, to: colored.count, by: 4) where colored[i] == 235 {
        colored[i] = 235; colored[i + 1] = 175; colored[i + 2] = 80
    }
    let colorMask = try PriceMask(bytes: colored, width: w, height: h)
    let targetWidth = 280, targetHeight = 250
    var shifted = [UInt8](repeating: 0, count: targetWidth * targetHeight * 4)
    for y in 0..<h { for x in 0..<w {
        let a = (y * w + x) * 4, b = ((y + 9) * targetWidth + x + 47) * 4
        shifted[b] = colored[a + 2]; shifted[b + 1] = colored[a + 1]
        shifted[b + 2] = colored[a]; shifted[b + 3] = 255
    } }
    let moved = shifted.withUnsafeBufferPointer { colorMask.locate($0.baseAddress!, targetWidth, targetHeight, targetWidth * 4, near: (0, 0)) }
    guard moved["window_x"] as? Int == 47 && moved["window_y"] as? Int == 9 else {
        throw ProbeError.invalid("colored BGRA/moving template position failed")
    }
    print("frame-stream self-test: clean and shifted colored text accepted; black, dim and white blanks rejected")
}

final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
    let queue = DispatchQueue(label: "stockfloat.frame-stream")
    let out: URL
    let writer: AVAssetWriter
    let input: AVAssetWriterInput
    let adaptor: AVAssetWriterInputPixelBufferAdaptor
    var frames: [[String: Any]] = []
    var errors: [String] = []
    var firstPTS: CMTime?
    var ready = false

    init(out: URL, width: Int, height: Int) throws {
        self.out = out
        writer = try AVAssetWriter(outputURL: out.appendingPathComponent("capture.mov"), fileType: .mov)
        input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: width, AVVideoHeightKey: height,
            AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 4_000_000,
                                              AVVideoMaxKeyFrameIntervalKey: 30]])
        input.expectsMediaDataInRealTime = true
        adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input,
            sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
        super.init()
        guard writer.canAdd(input) else { throw ProbeError.invalid("video input rejected") }
        writer.add(input)
        guard writer.startWriting() else { throw ProbeError.invalid("video writer did not start") }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        queue.async { self.errors.append("stream stopped: \(error)") }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen else { return }
        let delivered = hostSeconds()
        let attachments = CMSampleBufferGetSampleAttachmentsArray(sample, createIfNecessary: false)
            as? [[SCStreamFrameInfo: Any]]
        let meta = attachments?.first ?? [:]
        let status = (meta[.status] as? NSNumber)?.intValue ?? -1
        let displayTicks = (meta[.displayTime] as? NSNumber)?.uint64Value
        var info = mach_timebase_info_data_t()
        mach_timebase_info(&info)
        let display = displayTicks.map { Double($0) * Double(info.numer) / Double(info.denom) / 1e9 }
        let pts = CMSampleBufferGetPresentationTimeStamp(sample)
        var row: [String: Any] = ["delivered_host": delivered, "status": status,
                                  "pts": CMTimeGetSeconds(pts), "encoded": false]
        if let display { row["display_host"] = display }
        if status == SCFrameStatus.complete.rawValue {
            if let buffer = CMSampleBufferGetImageBuffer(sample) {
                if firstPTS == nil {
                    firstPTS = pts
                    writer.startSession(atSourceTime: pts)
                }
                if input.isReadyForMoreMediaData {
                    row["encoded"] = adaptor.append(buffer, withPresentationTime: pts)
                    if row["encoded"] as? Bool != true { errors.append("video frame append failed") }
                } else {
                    errors.append("video writer backpressure dropped a source frame")
                }
                if !ready && row["encoded"] as? Bool == true {
                    ready = true
                    do {
                        try writeJSON(["host_time": delivered, "pts": CMTimeGetSeconds(pts),
                                       "display_host": display as Any? ?? NSNull()],
                                      out.appendingPathComponent("ready.json"))
                    } catch { errors.append("could not persist readiness: \(error)") }
                }
            } else { errors.append("complete sample has no pixel buffer") }
        }
        frames.append(row)
    }

    func finish() async throws {
        queue.sync { input.markAsFinished() }
        await writer.finishWriting()
        if writer.status != .completed { errors.append("video incomplete: \(String(describing: writer.error))") }
        try writeJSON(["frames": frames, "errors": errors, "ready": ready,
                       "finished_host": hostSeconds()], out.appendingPathComponent("source-frames.json"))
    }
}

@main
struct FrameStream {
    static func main() async {
        do {
            let args = CommandLine.arguments
            if args.count == 2 && args[1] == "--self-test" { try selfTest(); return }
            if args.count == 5 && args[1] == "--decode" {
                try await decode(URL(fileURLWithPath: args[2]), URL(fileURLWithPath: args[3]), URL(fileURLWithPath: args[4]))
                return
            }
            guard args.count == 3 else { throw ProbeError.invalid("usage: native_frame_stream CONFIG.json OUTDIR") }
            let configURL = URL(fileURLWithPath: args[1])
            let out = URL(fileURLWithPath: args[2], isDirectory: true)
            try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
            let config = try JSONSerialization.jsonObject(with: Data(contentsOf: configURL)) as? [String: Any] ?? [:]
            guard let rect = config["rect"] as? [Double], rect.count == 4,
                  let seconds = config["seconds"] as? Double, seconds > 0, seconds <= 60 else {
                throw ProbeError.invalid("invalid recording configuration")
            }
            guard CGPreflightScreenCaptureAccess() else {
                throw ProbeError.invalid("screen recording permission unavailable; not requesting access automatically")
            }
            let available = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
            let region = CGRect(x: rect[0], y: rect[1], width: rect[2], height: rect[3])
            guard let display = available.displays.first(where: { $0.frame.contains(region) }) else {
                throw ProbeError.invalid("recording region must fit one display")
            }
            let settings = SCStreamConfiguration()
            settings.sourceRect = region.offsetBy(dx: -display.frame.minX, dy: -display.frame.minY)
            settings.width = Int(region.width)
            settings.height = Int(region.height)
            settings.minimumFrameInterval = CMTime(value: 1, timescale: 60)
            settings.queueDepth = 8
            settings.pixelFormat = kCVPixelFormatType_32BGRA
            settings.showsCursor = false
            settings.capturesAudio = false
            let sink = try Recorder(out: out, width: settings.width, height: settings.height)
            let filter = SCContentFilter(display: display, excludingWindows: [])
            let stream = SCStream(filter: filter, configuration: settings, delegate: sink)
            try stream.addStreamOutput(sink, type: .screen, sampleHandlerQueue: sink.queue)
            try await stream.startCapture()
            let deadline = hostSeconds() + seconds
            while hostSeconds() < deadline && !FileManager.default.fileExists(atPath: out.appendingPathComponent("stop").path) {
                try await Task.sleep(nanoseconds: 20_000_000)
            }
            try await stream.stopCapture()
            try await sink.finish()
            if !sink.ready || !sink.errors.isEmpty { throw ProbeError.invalid("recording has missing or invalid frames") }
        } catch {
            FileHandle.standardError.write(Data("frame-stream: \(error)\n".utf8))
            exit(1)
        }
    }
}
