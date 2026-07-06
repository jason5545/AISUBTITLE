import AVFAudio
import CoreMedia
import Foundation

final class PCMConverter {
    private let outputFormat: AVAudioFormat
    private var inputFormat: AVAudioFormat?
    private var converter: AVAudioConverter?

    init(sampleRate: Int, channelCount: Int) throws {
        guard let outputFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: Double(sampleRate),
            channels: AVAudioChannelCount(channelCount),
            interleaved: true
        ) else {
            throw AppError.audioConversionFailed("Could not create output PCM format.")
        }
        self.outputFormat = outputFormat
    }

    func convert(_ sampleBuffer: CMSampleBuffer) throws -> Data {
        guard let formatDescription = CMSampleBufferGetFormatDescription(sampleBuffer) else {
            throw AppError.audioFormatUnavailable
        }

        let currentInputFormat = AVAudioFormat(cmAudioFormatDescription: formatDescription)
        let frameCount = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
        guard frameCount > 0 else {
            return Data()
        }

        guard let inputBuffer = AVAudioPCMBuffer(pcmFormat: currentInputFormat, frameCapacity: frameCount) else {
            throw AppError.audioConversionFailed("Could not allocate input buffer.")
        }
        inputBuffer.frameLength = frameCount

        let copyStatus = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sampleBuffer,
            at: 0,
            frameCount: Int32(frameCount),
            into: inputBuffer.mutableAudioBufferList
        )
        guard copyStatus == noErr else {
            throw AppError.audioConversionFailed("CMSampleBuffer copy returned \(copyStatus).")
        }

        let converter = try converterForInputFormat(currentInputFormat)
        let ratio = outputFormat.sampleRate / currentInputFormat.sampleRate
        let outputCapacity = max(1, AVAudioFrameCount((Double(frameCount) * ratio).rounded(.up)) + 32)

        guard let outputBuffer = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: outputCapacity) else {
            throw AppError.audioConversionFailed("Could not allocate output buffer.")
        }

        try converter.convert(to: outputBuffer, from: inputBuffer)

        guard let channelData = outputBuffer.int16ChannelData else {
            throw AppError.audioConversionFailed("Converted buffer did not expose int16 samples.")
        }

        let byteCount = Int(outputBuffer.frameLength) * Int(outputFormat.channelCount) * MemoryLayout<Int16>.size
        return Data(bytes: channelData[0], count: byteCount)
    }

    private func converterForInputFormat(_ format: AVAudioFormat) throws -> AVAudioConverter {
        if let inputFormat, inputFormat == format, let converter {
            return converter
        }

        guard let converter = AVAudioConverter(from: format, to: outputFormat) else {
            throw AppError.audioConversionFailed("Could not create AVAudioConverter.")
        }

        self.inputFormat = format
        self.converter = converter
        return converter
    }
}
